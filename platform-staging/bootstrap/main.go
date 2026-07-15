package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	maxIndexBytes    = 256 * 1024
	maxManifestBytes = 1024 * 1024
	maxCoreBytes     = 150 * 1024 * 1024
	maxPackBytes     = 500 * 1024 * 1024
	maxFiles         = 50_000
)

var (
	safeID              = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	releaseIDPattern    = regexp.MustCompile(`^release-stable-[0-9a-f]{24}$`)
	sha256Pattern       = regexp.MustCompile(`^[0-9a-f]{64}$`)
	stableSemverPattern = regexp.MustCompile(`^1\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$`)
	semverPattern       = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$`)
)

// Set by the pinned Go build.  The external config may carry a signed release
// floor, but it cannot replace the trust key, helper identity or Control Plane
// endpoint selected by the reviewed Bootstrap build.
var (
	embeddedReleaseKeysSHA256     string
	embeddedSandboxHelperSHA256   string
	embeddedPublicIndexURLSHA256  string
	embeddedPublicationKeysSHA256 string
)

type config struct {
	SchemaVersion         int               `json:"schema_version"`
	PublicIndexURL        string            `json:"public_index_url"`
	ReleasePublicKeys     map[string]string `json:"release_public_keys"`
	PublicationPublicKeys map[string]string `json:"publication_public_keys"`
	SandboxHelperSHA256   string            `json:"sandbox_helper_sha256"`
	MinimumStable         *minimumStable    `json:"minimum_stable"`
}

type signature struct {
	Algorithm string `json:"algorithm"`
	KeyID     string `json:"key_id"`
	Value     string `json:"value"`
}

type minimumStable struct {
	Sequence  int64     `json:"sequence"`
	Version   string    `json:"version"`
	Signature signature `json:"signature"`
}

type source struct {
	SourceID string `json:"source_id"`
	Kind     string `json:"kind"`
	Priority int    `json:"priority"`
	BaseURL  string `json:"base_url"`
}

type indexSource struct {
	SourceID string `json:"source_id"`
	Kind     string `json:"kind"`
	Priority int    `json:"priority"`
	URL      string `json:"url"`
}

type indexManifest struct {
	FileName  string        `json:"file_name"`
	SHA256    string        `json:"sha256"`
	Signature signature     `json:"signature"`
	Sources   []indexSource `json:"sources"`
}

type indexArtifact struct {
	ArtifactID   string        `json:"artifact_id"`
	Platform     string        `json:"platform"`
	Architecture string        `json:"architecture"`
	FileName     string        `json:"file_name"`
	SizeBytes    int64         `json:"size_bytes"`
	SHA256       string        `json:"sha256"`
	Signature    signature     `json:"signature"`
	Sources      []indexSource `json:"sources"`
}

type indexRelease struct {
	ReleaseID                string          `json:"release_id"`
	Version                  string          `json:"version"`
	Channel                  string          `json:"channel"`
	CreatedAt                string          `json:"created_at"`
	BuildDigest              string          `json:"build_digest"`
	PublicationReceiptSHA256 string          `json:"publication_receipt_sha256"`
	Manifest                 indexManifest   `json:"manifest"`
	BootstrapArtifacts       []indexArtifact `json:"bootstrap_artifacts"`
}

type authorityTarget struct {
	ManifestSHA256 string `json:"manifest_sha256"`
	ReleaseID      string `json:"release_id"`
	Version        string `json:"version"`
	BuildDigest    string `json:"build_digest"`
}

type pointerAuthority struct {
	Sequence  int64           `json:"sequence"`
	Revision  string          `json:"revision"`
	Target    authorityTarget `json:"target"`
	Signature signature       `json:"signature"`
}

type pointerFreshness struct {
	AuthoritySHA256 string    `json:"authority_sha256"`
	IssuedAt        string    `json:"issued_at"`
	ExpiresAt       string    `json:"expires_at"`
	Signature       signature `json:"signature"`
}

type publicIndex struct {
	SchemaVersion int               `json:"schema_version"`
	DocumentType  string            `json:"document_type"`
	Trust         string            `json:"trust"`
	Status        string            `json:"status"`
	Authority     *pointerAuthority `json:"authority"`
	Freshness     *pointerFreshness `json:"freshness"`
	Release       *indexRelease     `json:"release"`
}

type pointerState struct {
	SchemaVersion int              `json:"schema_version"`
	Authority     pointerAuthority `json:"authority"`
	Freshness     pointerFreshness `json:"freshness"`
}

type localConfig struct {
	SchemaVersion    int    `json:"schema_version"`
	LegacyV030Source string `json:"legacy_v030_source"`
}

type artifact struct {
	ArtifactID   string    `json:"artifact_id"`
	Platform     string    `json:"platform"`
	Architecture string    `json:"architecture"`
	FileName     string    `json:"file_name"`
	SizeBytes    int64     `json:"size_bytes"`
	SHA256       string    `json:"sha256"`
	Signature    signature `json:"signature"`
}

type manifest struct {
	SchemaVersion int        `json:"schema_version"`
	ReleaseID     string     `json:"release_id"`
	Version       string     `json:"version"`
	BuildDigest   string     `json:"build_digest"`
	Channel       string     `json:"channel"`
	CreatedAt     string     `json:"created_at"`
	Sources       []source   `json:"sources"`
	Artifacts     []artifact `json:"artifacts"`
	Signature     signature  `json:"signature"`
}

// Field order is deliberately lexical to match Python sort_keys=True.
type canonicalManifest struct {
	Artifacts     []canonicalArtifact `json:"artifacts"`
	BuildDigest   string              `json:"build_digest"`
	Channel       string              `json:"channel"`
	CreatedAt     string              `json:"created_at"`
	ReleaseID     string              `json:"release_id"`
	SchemaVersion int                 `json:"schema_version"`
	Sources       []canonicalSource   `json:"sources"`
	Version       string              `json:"version"`
}

type canonicalArtifact struct {
	Architecture string    `json:"architecture"`
	ArtifactID   string    `json:"artifact_id"`
	FileName     string    `json:"file_name"`
	Platform     string    `json:"platform"`
	SHA256       string    `json:"sha256"`
	Signature    signature `json:"signature"`
	SizeBytes    int64     `json:"size_bytes"`
}

type canonicalSource struct {
	BaseURL  string `json:"base_url"`
	Kind     string `json:"kind"`
	Priority int    `json:"priority"`
	SourceID string `json:"source_id"`
}

type installResult struct {
	SchemaVersion int    `json:"schema_version"`
	State         string `json:"state"`
	TransactionID string `json:"transaction_id"`
	SlotID        string `json:"slot_id"`
}

type boundedBuffer struct {
	buffer   bytes.Buffer
	limit    int
	overflow bool
}

func (buffer *boundedBuffer) Write(payload []byte) (int, error) {
	original := len(payload)
	if buffer.overflow || original > buffer.limit-buffer.buffer.Len() {
		remaining := buffer.limit - buffer.buffer.Len()
		if remaining > 0 {
			_, _ = buffer.buffer.Write(payload[:remaining])
		}
		buffer.overflow = true
		return max(remaining, 0), fmt.Errorf("subprocess output exceeded its bound")
	}
	_, _ = buffer.buffer.Write(payload)
	return original, nil
}

func (buffer *boundedBuffer) Bytes() []byte {
	return buffer.buffer.Bytes()
}

func main() {
	selfTest := flag.Bool("self-test", false, "verify the packaged bootstrap entrypoint")
	indexURL := flag.String("index", "", "override the public discovery URL")
	installRoot := flag.String("install-root", "", "override the EcoreX data root")
	flag.Parse()
	if *selfTest {
		platform, architecture, err := productTarget()
		if err != nil {
			fail(err)
		}
		writeJSON(map[string]any{"schema_version": 1, "status": "passed", "platform": platform, "architecture": architecture})
		return
	}
	if err := run(*indexURL, *installRoot); err != nil {
		fail(err)
	}
}

func run(indexOverride, rootOverride string) error {
	configuration, _, keys, publicationKeys, err := loadConfig()
	if err != nil {
		return err
	}
	indexLocation := configuration.PublicIndexURL
	if indexOverride != "" {
		if indexOverride != configuration.PublicIndexURL {
			return fmt.Errorf("public discovery override is outside the trusted endpoint")
		}
		indexLocation = indexOverride
	}
	if err := validateHTTPS(indexLocation); err != nil {
		return fmt.Errorf("public discovery URL is invalid")
	}
	root, err := installRoot(rootOverride)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return fmt.Errorf("install root is unavailable")
	}
	lock, err := acquireProductLock(filepath.Join(root, "bootstrap-launch.lock"))
	if err != nil {
		return err
	}
	defer lock.close()
	if err := ensureBootstrapStateDirectory(root); err != nil {
		return err
	}
	bootstrapHelper, err := stageSandboxHelper(root, configuration.SandboxHelperSHA256)
	if err != nil {
		return err
	}
	legacySource, err := loadTrustedLocalConfig(root)
	if err != nil {
		return err
	}

	client := newHTTPClient()
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Minute)
	defer cancel()
	indexBytes, trustedNow, err := fetchDiscovery(ctx, client, indexLocation, maxIndexBytes)
	if err != nil {
		return fmt.Errorf("public discovery is unavailable")
	}
	var index publicIndex
	if err := decodeExact(indexBytes, &index); err != nil || index.SchemaVersion != 1 || index.DocumentType != "ecorex.public-bootstrap-discovery" || index.Trust != "untrusted-discovery-hint" || index.Status != "published" || index.Authority == nil || index.Freshness == nil || index.Release == nil {
		return fmt.Errorf("public discovery contract is invalid")
	}
	if err := validatePointerAuthority(&index, keys, *configuration.MinimumStable); err != nil {
		return err
	}
	if err := validatePointerFreshness(*index.Authority, *index.Freshness, publicationKeys, trustedNow); err != nil {
		return err
	}
	manifestBytes, err := fetchManifest(ctx, client, index.Release.Manifest)
	if err != nil {
		return err
	}
	var release manifest
	if err := decodeExact(manifestBytes, &release); err != nil {
		return fmt.Errorf("release manifest is invalid")
	}
	if err := validateManifest(&release, index.Release, keys); err != nil {
		return err
	}
	if err := acceptPointerAuthority(root, *index.Authority, *index.Freshness, keys, publicationKeys, trustedNow); err != nil {
		return err
	}
	platform, architecture, err := productTarget()
	if err != nil {
		return err
	}
	selected, err := requiredArtifacts(&release, platform, architecture)
	if err != nil {
		return err
	}
	work := filepath.Join(root, "bootstrap-work", release.ReleaseID)
	if err := os.MkdirAll(work, 0o700); err != nil {
		return fmt.Errorf("bootstrap workspace is unavailable")
	}
	artifactsDir := filepath.Join(work, "artifacts")
	if err := os.MkdirAll(artifactsDir, 0o700); err != nil {
		return fmt.Errorf("bootstrap artifact directory is unavailable")
	}
	for _, item := range selected {
		destination := filepath.Join(artifactsDir, item.FileName)
		if err := downloadArtifact(ctx, client, &release, item, destination, keys); err != nil {
			return err
		}
	}
	manifestPath := filepath.Join(artifactsDir, "release-manifest.json")
	if err := atomicWrite(manifestPath, manifestBytes, 0o600); err != nil {
		return fmt.Errorf("release manifest could not be staged")
	}
	core := selected[0]
	coreRoot := filepath.Join(work, "core")
	_ = os.RemoveAll(coreRoot)
	if err := extractCore(filepath.Join(artifactsDir, core.FileName), coreRoot); err != nil {
		return err
	}
	trustedDefinitions, err := persistTrust(root, configuration.ReleasePublicKeys, keys)
	if err != nil {
		return err
	}
	result, err := installLocal(coreRoot, root, manifestPath, artifactsDir, trustedDefinitions, bootstrapHelper, configuration.SandboxHelperSHA256)
	if err != nil {
		return err
	}
	installedPayload := filepath.Join(root, "slots", result.SlotID, "payload")
	python := filepath.Join(installedPayload, "bin", "pack-python", "bin", "python3")
	if runtime.GOOS == "windows" {
		python = filepath.Join(installedPayload, "bin", "pack-python", "python.exe")
	}
	if err := os.RemoveAll(work); err != nil {
		return fmt.Errorf("bootstrap workspace cleanup failed")
	}
	return supervise(python, root, trustedDefinitions, legacySource)
}

func productTarget() (string, string, error) {
	platform := ""
	architecture := ""
	switch runtime.GOOS {
	case "windows":
		platform = "windows"
	case "darwin":
		platform = "macos"
	default:
		return "", "", fmt.Errorf("this Bootstrap does not support the host platform")
	}
	switch runtime.GOARCH {
	case "amd64":
		architecture = "x64"
	case "arm64":
		architecture = "arm64"
	default:
		return "", "", fmt.Errorf("this Bootstrap does not support the host architecture")
	}
	if platform == "windows" && architecture != "x64" {
		return "", "", fmt.Errorf("Windows Bootstrap requires x64")
	}
	return platform, architecture, nil
}

func loadConfig() (config, string, map[string]ed25519.PublicKey, map[string]ed25519.PublicKey, error) {
	executable, err := os.Executable()
	if err != nil {
		return config{}, "", nil, nil, fmt.Errorf("Bootstrap path is unavailable")
	}
	configPath := filepath.Clean(filepath.Join(filepath.Dir(executable), "..", "bootstrap-config.json"))
	payload, err := os.ReadFile(configPath)
	if err != nil || len(payload) == 0 || len(payload) > 64*1024 {
		return config{}, "", nil, nil, fmt.Errorf("Bootstrap configuration is unavailable")
	}
	var value config
	if err := decodeExact(payload, &value); err != nil || value.SchemaVersion != 1 || validateHTTPS(value.PublicIndexURL) != nil || value.MinimumStable == nil {
		return config{}, "", nil, nil, fmt.Errorf("Bootstrap configuration is invalid")
	}
	keys, err := decodePublicKeys(value.ReleasePublicKeys)
	if err != nil {
		return config{}, "", nil, nil, fmt.Errorf("Bootstrap trust configuration is invalid")
	}
	publicationKeys, err := decodePublicKeys(value.PublicationPublicKeys)
	if err != nil {
		return config{}, "", nil, nil, fmt.Errorf("Bootstrap publication trust is invalid")
	}
	for keyID, releaseKey := range keys {
		if _, duplicated := publicationKeys[keyID]; duplicated {
			return config{}, "", nil, nil, fmt.Errorf("Bootstrap signing roles are not separated")
		}
		for _, publicationKey := range publicationKeys {
			if string(releaseKey) == string(publicationKey) {
				return config{}, "", nil, nil, fmt.Errorf("Bootstrap signing roles are not separated")
			}
		}
	}
	encodedKeys, err := json.Marshal(value.ReleasePublicKeys)
	if err != nil || !sha256Pattern.MatchString(embeddedReleaseKeysSHA256) || sha256Hex(encodedKeys) != embeddedReleaseKeysSHA256 {
		return config{}, "", nil, nil, fmt.Errorf("Bootstrap trust configuration is not build-bound")
	}
	encodedPublicationKeys, err := json.Marshal(value.PublicationPublicKeys)
	if err != nil || !sha256Pattern.MatchString(embeddedPublicationKeysSHA256) || sha256Hex(encodedPublicationKeys) != embeddedPublicationKeysSHA256 {
		return config{}, "", nil, nil, fmt.Errorf("Bootstrap publication trust is not build-bound")
	}
	if !sha256Pattern.MatchString(embeddedPublicIndexURLSHA256) || sha256Hex([]byte(value.PublicIndexURL)) != embeddedPublicIndexURLSHA256 {
		return config{}, "", nil, nil, fmt.Errorf("Bootstrap Control Plane endpoint is not build-bound")
	}
	if runtime.GOOS == "windows" {
		if !sha256Pattern.MatchString(value.SandboxHelperSHA256) || value.SandboxHelperSHA256 != embeddedSandboxHelperSHA256 {
			return config{}, "", nil, nil, fmt.Errorf("Bootstrap sandbox helper identity is invalid")
		}
	} else if value.SandboxHelperSHA256 != "" || embeddedSandboxHelperSHA256 != "none" {
		return config{}, "", nil, nil, fmt.Errorf("Bootstrap sandbox helper identity is invalid")
	}
	if err := validateMinimumStable(*value.MinimumStable, keys); err != nil {
		return config{}, "", nil, nil, err
	}
	return value, configPath, keys, publicationKeys, nil
}

func decodePublicKeys(encodedKeys map[string]string) (map[string]ed25519.PublicKey, error) {
	if len(encodedKeys) < 1 || len(encodedKeys) > 8 {
		return nil, fmt.Errorf("public keyring size is invalid")
	}
	keys := make(map[string]ed25519.PublicKey, len(encodedKeys))
	for keyID, encoded := range encodedKeys {
		if !safeID.MatchString(keyID) {
			return nil, fmt.Errorf("public key identity is invalid")
		}
		raw, err := base64.StdEncoding.Strict().DecodeString(encoded)
		if err != nil || len(raw) != ed25519.PublicKeySize || base64.StdEncoding.EncodeToString(raw) != encoded {
			return nil, fmt.Errorf("public key bytes are invalid")
		}
		keys[keyID] = ed25519.PublicKey(raw)
	}
	return keys, nil
}

func stageSandboxHelper(root, expectedDigest string) (string, error) {
	if runtime.GOOS != "windows" {
		if expectedDigest != "" {
			return "", fmt.Errorf("unexpected sandbox helper identity")
		}
		return "", nil
	}
	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("Bootstrap path is unavailable")
	}
	source := filepath.Join(filepath.Dir(executable), "ecorex-sandbox-host.exe")
	metadata, err := os.Lstat(source)
	if err != nil || !metadata.Mode().IsRegular() || metadata.Mode()&os.ModeSymlink != 0 || metadata.Size() < 1 || metadata.Size() > 2*1024*1024 || !fileMatches(source, metadata.Size(), expectedDigest) {
		return "", fmt.Errorf("Bootstrap sandbox helper is untrusted")
	}
	payload, err := os.ReadFile(source)
	if err != nil || int64(len(payload)) != metadata.Size() {
		return "", fmt.Errorf("Bootstrap sandbox helper is unreadable")
	}
	directory := filepath.Join(root, "bootstrap", "bin")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return "", fmt.Errorf("Bootstrap helper directory is unavailable")
	}
	for _, candidate := range []string{root, filepath.Join(root, "bootstrap"), directory} {
		info, inspectErr := os.Lstat(candidate)
		resolved, resolveErr := filepath.EvalSymlinks(candidate)
		absolute, absoluteErr := filepath.Abs(candidate)
		resolvedAbsolute, resolvedAbsoluteErr := filepath.Abs(resolved)
		if inspectErr != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || resolveErr != nil || absoluteErr != nil || resolvedAbsoluteErr != nil || !strings.EqualFold(filepath.Clean(absolute), filepath.Clean(resolvedAbsolute)) {
			return "", fmt.Errorf("Bootstrap helper directory is unsafe")
		}
	}
	destination := filepath.Join(directory, "ecorex-sandbox-host.exe")
	if info, inspectErr := os.Lstat(destination); inspectErr == nil {
		if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 {
			return "", fmt.Errorf("Bootstrap sandbox helper destination is unsafe")
		}
	} else if !errors.Is(inspectErr, os.ErrNotExist) {
		return "", fmt.Errorf("Bootstrap sandbox helper destination is unavailable")
	}
	if err := atomicWrite(destination, payload, 0o700); err != nil || !fileMatches(destination, metadata.Size(), expectedDigest) {
		return "", fmt.Errorf("Bootstrap sandbox helper could not be staged")
	}
	return destination, nil
}

func ensureBootstrapStateDirectory(root string) error {
	rootAbsolute, err := filepath.Abs(filepath.Clean(root))
	if err != nil {
		return fmt.Errorf("install root is invalid")
	}
	resolvedRoot, err := filepath.EvalSymlinks(rootAbsolute)
	if err != nil || !samePath(rootAbsolute, resolvedRoot) {
		return fmt.Errorf("install root contains a link or reparse point")
	}
	directory := filepath.Join(rootAbsolute, "bootstrap")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return fmt.Errorf("Bootstrap state directory is unavailable")
	}
	for _, candidate := range []string{rootAbsolute, directory} {
		metadata, inspectErr := os.Lstat(candidate)
		resolved, resolveErr := filepath.EvalSymlinks(candidate)
		if inspectErr != nil || !metadata.IsDir() || metadata.Mode()&os.ModeSymlink != 0 || resolveErr != nil || !samePath(candidate, resolved) {
			return fmt.Errorf("Bootstrap state directory is unsafe")
		}
	}
	return nil
}

func installRoot(override string) (string, error) {
	if override != "" {
		if !filepath.IsAbs(override) {
			return "", fmt.Errorf("install root must be absolute")
		}
		return filepath.Clean(override), nil
	}
	if runtime.GOOS == "windows" {
		base := os.Getenv("LOCALAPPDATA")
		if base == "" || !filepath.IsAbs(base) {
			return "", fmt.Errorf("Windows LocalAppData is unavailable")
		}
		return filepath.Join(base, "EcoreX"), nil
	}
	home, err := os.UserHomeDir()
	if err != nil || !filepath.IsAbs(home) {
		return "", fmt.Errorf("user data directory is unavailable")
	}
	return filepath.Join(home, "Library", "Application Support", "EcoreX"), nil
}

func loadTrustedLocalConfig(root string) (string, error) {
	configPath := filepath.Join(root, "bootstrap", "bootstrap-local.json")
	metadata, err := os.Lstat(configPath)
	if errors.Is(err, os.ErrNotExist) {
		return "", nil
	}
	if err != nil || !metadata.Mode().IsRegular() || metadata.Mode()&os.ModeSymlink != 0 || metadata.Size() < 1 || metadata.Size() > 16*1024 {
		return "", fmt.Errorf("local Bootstrap configuration is unsafe")
	}
	if err := validateTrustedLocalConfigFile(configPath); err != nil {
		return "", fmt.Errorf("local Bootstrap configuration is not administrator-owned")
	}
	file, err := os.Open(configPath)
	if err != nil {
		return "", fmt.Errorf("local Bootstrap configuration is unreadable")
	}
	payload, readErr := io.ReadAll(io.LimitReader(file, 16*1024+1))
	closeErr := file.Close()
	after, statErr := os.Lstat(configPath)
	if readErr != nil || closeErr != nil || statErr != nil || !os.SameFile(metadata, after) || len(payload) < 1 || len(payload) > 16*1024 || validateTrustedLocalConfigFile(configPath) != nil {
		return "", fmt.Errorf("local Bootstrap configuration changed while reading")
	}
	var value localConfig
	if decodeExact(payload, &value) != nil || value.SchemaVersion != 1 || value.LegacyV030Source == "" {
		return "", fmt.Errorf("local Bootstrap configuration is invalid")
	}
	source, err := canonicalLegacySource(value.LegacyV030Source, root)
	if err != nil {
		return "", err
	}
	return source, nil
}

func canonicalLegacySource(value, root string) (string, error) {
	if !filepath.IsAbs(value) || strings.ContainsAny(value, "\x00\r\n") {
		return "", fmt.Errorf("legacy v0.3.0 source must be an absolute local path")
	}
	absolute, err := filepath.Abs(filepath.Clean(value))
	if err != nil {
		return "", fmt.Errorf("legacy v0.3.0 source is invalid")
	}
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return "", fmt.Errorf("legacy v0.3.0 source is unavailable")
	}
	resolved, err = filepath.Abs(resolved)
	if err != nil || !samePath(absolute, resolved) {
		return "", fmt.Errorf("legacy v0.3.0 source contains a link or reparse point")
	}
	current := absolute
	for {
		metadata, inspectErr := os.Lstat(current)
		if inspectErr != nil || !metadata.IsDir() || metadata.Mode()&os.ModeSymlink != 0 {
			return "", fmt.Errorf("legacy v0.3.0 source path is unsafe")
		}
		if current == filepath.Dir(current) {
			break
		}
		current = filepath.Dir(current)
	}
	rootAbsolute, err := filepath.Abs(filepath.Clean(root))
	if err != nil || pathsOverlap(absolute, rootAbsolute) {
		return "", fmt.Errorf("legacy v0.3.0 source overlaps the v1 install root")
	}
	return absolute, nil
}

func pathsOverlap(left, right string) bool {
	return pathContains(left, right) || pathContains(right, left)
}

func pathContains(root, candidate string) bool {
	relative, err := filepath.Rel(root, candidate)
	return err == nil && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator))
}

func samePath(left, right string) bool {
	if runtime.GOOS == "windows" {
		return strings.EqualFold(filepath.Clean(left), filepath.Clean(right))
	}
	return filepath.Clean(left) == filepath.Clean(right)
}

func newHTTPClient() *http.Client {
	dialer := &net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}
	transport := &http.Transport{
		Proxy:                 nil,
		DialContext:           dialer.DialContext,
		ForceAttemptHTTP2:     true,
		DisableCompression:    true,
		TLSClientConfig:       &tls.Config{MinVersion: tls.VersionTLS12},
		TLSHandshakeTimeout:   15 * time.Second,
		ResponseHeaderTimeout: 30 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		MaxIdleConns:          4,
		MaxIdleConnsPerHost:   1,
	}
	return &http.Client{
		Transport:     transport,
		Timeout:       10 * time.Minute,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
	}
}

func fetchBounded(ctx context.Context, client *http.Client, location string, limit int64) ([]byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, location, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept-Encoding", "identity")
	response, err := client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || response.Header.Get("Content-Encoding") != "" && !strings.EqualFold(response.Header.Get("Content-Encoding"), "identity") {
		return nil, fmt.Errorf("unexpected HTTPS response")
	}
	if response.ContentLength > limit {
		return nil, fmt.Errorf("HTTPS response exceeds its bound")
	}
	reader := io.LimitReader(response.Body, limit+1)
	payload, err := io.ReadAll(reader)
	if err != nil || len(payload) == 0 || int64(len(payload)) > limit {
		return nil, fmt.Errorf("HTTPS response is incomplete or oversized")
	}
	return payload, nil
}

func fetchDiscovery(
	ctx context.Context,
	client *http.Client,
	location string,
	limit int64,
) ([]byte, time.Time, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, location, nil)
	if err != nil {
		return nil, time.Time{}, err
	}
	request.Header.Set("Accept-Encoding", "identity")
	response, err := client.Do(request)
	if err != nil {
		return nil, time.Time{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || response.Header.Get("Content-Encoding") != "" && !strings.EqualFold(response.Header.Get("Content-Encoding"), "identity") || response.ContentLength > limit {
		return nil, time.Time{}, fmt.Errorf("unexpected HTTPS discovery response")
	}
	serverTime, err := http.ParseTime(response.Header.Get("Date"))
	if err != nil {
		return nil, time.Time{}, fmt.Errorf("trusted discovery clock is unavailable")
	}
	serverTime = serverTime.UTC().Truncate(time.Second)
	localDelta := time.Now().UTC().Sub(serverTime)
	if localDelta < -24*time.Hour || localDelta > 24*time.Hour {
		return nil, time.Time{}, fmt.Errorf("local and trusted discovery clocks disagree")
	}
	payload, err := io.ReadAll(io.LimitReader(response.Body, limit+1))
	if err != nil || len(payload) == 0 || int64(len(payload)) > limit {
		return nil, time.Time{}, fmt.Errorf("HTTPS discovery response is incomplete or oversized")
	}
	return payload, serverTime, nil
}

func fetchManifest(ctx context.Context, client *http.Client, descriptor indexManifest) ([]byte, error) {
	if descriptor.FileName != "release-manifest.json" || !sha256Pattern.MatchString(descriptor.SHA256) || len(descriptor.Sources) != 3 {
		return nil, fmt.Errorf("manifest discovery contract is invalid")
	}
	var last error
	expectedKinds := []string{"github-cn-mirror", "github-release", "ecorex-cdn"}
	for priority, candidate := range descriptor.Sources {
		if candidate.Priority != priority || candidate.Kind != expectedKinds[priority] || !safeID.MatchString(candidate.SourceID) || validateHTTPS(candidate.URL) != nil {
			return nil, fmt.Errorf("manifest discovery source is invalid")
		}
		payload, err := fetchBounded(ctx, client, candidate.URL, maxManifestBytes)
		if err != nil {
			last = err
			continue
		}
		digest := sha256.Sum256(payload)
		if hex.EncodeToString(digest[:]) != descriptor.SHA256 {
			last = fmt.Errorf("manifest digest mismatch")
			continue
		}
		return payload, nil
	}
	return nil, fmt.Errorf("all signed manifest sources failed: %w", last)
}

func validateManifest(value *manifest, discovery *indexRelease, keys map[string]ed25519.PublicKey) error {
	if value.SchemaVersion != 1 || !safeID.MatchString(value.ReleaseID) || !semverPattern.MatchString(value.Version) || !sha256Pattern.MatchString(value.BuildDigest) || value.Channel != "stable" || len(value.Sources) != 3 || len(value.Artifacts) == 0 || len(value.Artifacts) > 64 {
		return fmt.Errorf("release manifest contract is invalid")
	}
	if discovery.ReleaseID != value.ReleaseID || discovery.Version != value.Version || discovery.Channel != value.Channel || discovery.BuildDigest != value.BuildDigest || discovery.Manifest.Signature != value.Signature {
		return fmt.Errorf("public discovery does not match the signed release")
	}
	expectedKinds := []string{"github-cn-mirror", "github-release", "ecorex-cdn"}
	hosts := map[string]bool{}
	for index, item := range value.Sources {
		if item.Priority != index || item.Kind != expectedKinds[index] || !safeID.MatchString(item.SourceID) || validateHTTPS(item.BaseURL) != nil {
			return fmt.Errorf("release source contract is invalid")
		}
		parsed, _ := url.Parse(item.BaseURL)
		host := strings.ToLower(parsed.Hostname())
		if hosts[host] {
			return fmt.Errorf("release sources do not provide independent failover")
		}
		hosts[host] = true
		discovered := discovery.Manifest.Sources[index]
		expectedManifestURL := strings.TrimRight(item.BaseURL, "/") + "/release-manifest.json"
		if discovered.SourceID != item.SourceID || discovered.Kind != item.Kind || discovered.Priority != item.Priority || discovered.URL != expectedManifestURL {
			return fmt.Errorf("public discovery source does not match the signed release")
		}
	}
	payload, err := canonicalManifestPayload(value)
	if err != nil || verifySignature(payload, value.Signature, keys) != nil {
		return fmt.Errorf("release manifest signature is invalid")
	}
	seen := map[string]bool{}
	for _, item := range value.Artifacts {
		if !safeID.MatchString(item.ArtifactID) || !safeFileName(item.FileName) || !sha256Pattern.MatchString(item.SHA256) || item.SizeBytes < 1 || item.SizeBytes > maxPackBytes || seen[item.ArtifactID] {
			return fmt.Errorf("release artifact contract is invalid")
		}
		seen[item.ArtifactID] = true
		if strings.HasPrefix(item.ArtifactID, "core-") && item.SizeBytes > maxCoreBytes {
			return fmt.Errorf("Core artifact exceeds its signed product bound")
		}
		if strings.HasSuffix(item.ArtifactID, "-manifest") && item.SizeBytes > maxManifestBytes {
			return fmt.Errorf("artifact sidecar exceeds its signed product bound")
		}
	}
	return nil
}

func validatePointerAuthority(index *publicIndex, keys map[string]ed25519.PublicKey, floor minimumStable) error {
	if index == nil || index.Authority == nil || index.Release == nil {
		return fmt.Errorf("signed pointer authority is unavailable")
	}
	authority := *index.Authority
	if err := validateStandaloneAuthority(authority, keys); err != nil {
		return err
	}
	if err := validateMinimumStable(floor, keys); err != nil || authority.Sequence < floor.Sequence {
		return fmt.Errorf("signed pointer is below the Bootstrap minimum stable target")
	}
	release := index.Release
	if authority.Revision != release.ReleaseID || authority.Target != (authorityTarget{
		ManifestSHA256: release.Manifest.SHA256,
		ReleaseID:      release.ReleaseID,
		Version:        release.Version,
		BuildDigest:    release.BuildDigest,
	}) || release.Channel != "stable" {
		return fmt.Errorf("signed pointer target does not match public discovery")
	}
	return nil
}

func validateMinimumStable(value minimumStable, keys map[string]ed25519.PublicKey) error {
	expected, err := stableReleaseSequence(value.Version)
	if err != nil || value.Sequence != expected {
		return fmt.Errorf("Bootstrap minimum stable target is invalid")
	}
	payload := []byte(strings.Join([]string{
		"ecorex.bootstrap-minimum-stable.v1",
		strconv.FormatInt(value.Sequence, 10),
		value.Version,
	}, "\x00"))
	if verifySignature(payload, value.Signature, keys) != nil {
		return fmt.Errorf("Bootstrap minimum stable signature is invalid")
	}
	return nil
}

func validateStandaloneAuthority(value pointerAuthority, keys map[string]ed25519.PublicKey) error {
	if value.Sequence < 1 || value.Sequence > 999999999999 ||
		!releaseIDPattern.MatchString(value.Revision) ||
		value.Revision != value.Target.ReleaseID ||
		!releaseIDPattern.MatchString(value.Target.ReleaseID) ||
		!sha256Pattern.MatchString(value.Target.ManifestSHA256) ||
		!sha256Pattern.MatchString(value.Target.BuildDigest) {
		return fmt.Errorf("signed pointer authority contract is invalid")
	}
	expected, err := stableReleaseSequence(value.Target.Version)
	if err != nil || expected != value.Sequence {
		return fmt.Errorf("signed pointer sequence is invalid")
	}
	payload := pointerAuthorityPayload(value)
	if err := verifySignature(payload, value.Signature, keys); err != nil {
		return fmt.Errorf("signed pointer authority is invalid")
	}
	return nil
}

func pointerAuthorityPayload(value pointerAuthority) []byte {
	return []byte(strings.Join([]string{
		"ecorex.public-bootstrap-pointer-authority.v1",
		strconv.FormatInt(value.Sequence, 10),
		value.Revision,
		value.Target.ManifestSHA256,
		value.Target.ReleaseID,
		value.Target.Version,
		value.Target.BuildDigest,
	}, "\x00"))
}

func pointerAuthoritySHA256(value pointerAuthority) string {
	return sha256Hex(pointerAuthorityPayload(value))
}

func pointerFreshnessPayload(value pointerFreshness) []byte {
	return []byte(strings.Join([]string{
		"ecorex.public-bootstrap-freshness.v1",
		value.AuthoritySHA256,
		value.IssuedAt,
		value.ExpiresAt,
	}, "\x00"))
}

func parseCanonicalUTCSecond(value string) (time.Time, error) {
	if len(value) != len("2006-01-02T15:04:05Z") {
		return time.Time{}, fmt.Errorf("timestamp is not canonical UTC seconds")
	}
	parsed, err := time.Parse("2006-01-02T15:04:05Z", value)
	if err != nil || parsed.Format("2006-01-02T15:04:05Z") != value {
		return time.Time{}, fmt.Errorf("timestamp is not canonical UTC seconds")
	}
	return parsed.UTC(), nil
}

func validatePointerFreshnessEnvelope(
	authority pointerAuthority,
	value pointerFreshness,
	publicationKeys map[string]ed25519.PublicKey,
) (time.Time, time.Time, error) {
	if !sha256Pattern.MatchString(value.AuthoritySHA256) ||
		value.AuthoritySHA256 != pointerAuthoritySHA256(authority) ||
		value.Signature.KeyID == authority.Signature.KeyID {
		return time.Time{}, time.Time{}, fmt.Errorf("signed discovery freshness is not bound to the pointer authority")
	}
	issuedAt, issuedErr := parseCanonicalUTCSecond(value.IssuedAt)
	expiresAt, expiresErr := parseCanonicalUTCSecond(value.ExpiresAt)
	if issuedErr != nil || expiresErr != nil || !expiresAt.After(issuedAt) || expiresAt.Sub(issuedAt) > 24*time.Hour {
		return time.Time{}, time.Time{}, fmt.Errorf("signed discovery freshness lifetime is invalid")
	}
	if err := verifySignature(pointerFreshnessPayload(value), value.Signature, publicationKeys); err != nil {
		return time.Time{}, time.Time{}, fmt.Errorf("signed discovery freshness is invalid")
	}
	return issuedAt, expiresAt, nil
}

func validatePointerFreshness(
	authority pointerAuthority,
	value pointerFreshness,
	publicationKeys map[string]ed25519.PublicKey,
	trustedNow time.Time,
) error {
	issuedAt, expiresAt, err := validatePointerFreshnessEnvelope(
		authority, value, publicationKeys,
	)
	if err != nil {
		return err
	}
	now := trustedNow.UTC().Truncate(time.Second)
	if now.IsZero() || issuedAt.After(now.Add(5*time.Minute)) {
		return fmt.Errorf("signed discovery freshness is from the future; check system time and network")
	}
	if !now.Before(expiresAt) {
		return fmt.Errorf("signed discovery freshness expired; check system time and network")
	}
	return nil
}

func stableReleaseSequence(version string) (int64, error) {
	match := stableSemverPattern.FindStringSubmatch(version)
	if len(match) != 3 {
		return 0, fmt.Errorf("Bootstrap accepts stable v1 releases only")
	}
	minor, minorErr := strconv.ParseInt(match[1], 10, 64)
	patch, patchErr := strconv.ParseInt(match[2], 10, 64)
	if minorErr != nil || patchErr != nil {
		return 0, fmt.Errorf("stable release version is invalid")
	}
	sequence := minor*1_000_000 + patch + 1
	if sequence < 1 || sequence > 999999999999 {
		return 0, fmt.Errorf("stable release sequence is outside its product bound")
	}
	return sequence, nil
}

func acceptPointerAuthority(
	root string,
	candidate pointerAuthority,
	freshness pointerFreshness,
	keys map[string]ed25519.PublicKey,
	publicationKeys map[string]ed25519.PublicKey,
	trustedNow time.Time,
) error {
	if err := validateStandaloneAuthority(candidate, keys); err != nil {
		return err
	}
	if err := validatePointerFreshness(candidate, freshness, publicationKeys, trustedNow); err != nil {
		return err
	}
	statePath := filepath.Join(root, "bootstrap", "pointer-authority.json")
	if metadata, err := os.Lstat(statePath); err == nil {
		if !metadata.Mode().IsRegular() || metadata.Mode()&os.ModeSymlink != 0 || metadata.Size() < 1 || metadata.Size() > 64*1024 {
			return fmt.Errorf("persisted pointer authority is unsafe")
		}
		payload, readErr := os.ReadFile(statePath)
		var persisted pointerState
		if readErr != nil || decodeExact(payload, &persisted) != nil || persisted.SchemaVersion != 1 || validateStandaloneAuthority(persisted.Authority, keys) != nil {
			return fmt.Errorf("persisted pointer authority is invalid")
		}
		persistedIssuedAt, persistedExpiresAt, freshnessErr := validatePointerFreshnessEnvelope(
			persisted.Authority, persisted.Freshness, publicationKeys,
		)
		if freshnessErr != nil {
			return fmt.Errorf("persisted pointer freshness is invalid")
		}
		if candidate.Sequence < persisted.Authority.Sequence {
			return fmt.Errorf("signed pointer rollback was refused")
		}
		if candidate.Sequence == persisted.Authority.Sequence {
			if candidate.Revision != persisted.Authority.Revision || candidate.Target != persisted.Authority.Target {
				return fmt.Errorf("signed pointer sequence was replayed with another target")
			}
			candidateIssuedAt, candidateExpiresAt, _ := validatePointerFreshnessEnvelope(
				candidate, freshness, publicationKeys,
			)
			if freshness.AuthoritySHA256 == persisted.Freshness.AuthoritySHA256 &&
				candidateIssuedAt.Equal(persistedIssuedAt) &&
				candidateExpiresAt.Equal(persistedExpiresAt) {
				return nil
			}
			if !candidateIssuedAt.After(persistedIssuedAt) || !candidateExpiresAt.After(persistedExpiresAt) {
				return fmt.Errorf("signed pointer freshness did not advance monotonically")
			}
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("persisted pointer authority is unavailable")
	}
	payload, err := json.Marshal(pointerState{
		SchemaVersion: 1,
		Authority:     candidate,
		Freshness:     freshness,
	})
	if err != nil {
		return fmt.Errorf("signed pointer authority could not be persisted")
	}
	payload = append(payload, '\n')
	if err := atomicWrite(statePath, payload, 0o600); err != nil {
		return fmt.Errorf("signed pointer authority could not be persisted")
	}
	return nil
}

func canonicalManifestPayload(value *manifest) ([]byte, error) {
	artifacts := make([]canonicalArtifact, len(value.Artifacts))
	for index, item := range value.Artifacts {
		artifacts[index] = canonicalArtifact{item.Architecture, item.ArtifactID, item.FileName, item.Platform, item.SHA256, item.Signature, item.SizeBytes}
	}
	sources := make([]canonicalSource, len(value.Sources))
	for index, item := range value.Sources {
		sources[index] = canonicalSource{item.BaseURL, item.Kind, item.Priority, item.SourceID}
	}
	canonical := canonicalManifest{artifacts, value.BuildDigest, value.Channel, value.CreatedAt, value.ReleaseID, value.SchemaVersion, sources, value.Version}
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(canonical); err != nil {
		return nil, err
	}
	encoded := bytes.TrimSuffix(buffer.Bytes(), []byte("\n"))
	return append(append([]byte("ecorex-release-manifest-v1\n"), encoded...), '\n'), nil
}

func verifySignature(payload []byte, value signature, keys map[string]ed25519.PublicKey) error {
	if value.Algorithm != "ed25519" || !safeID.MatchString(value.KeyID) {
		return fmt.Errorf("signature contract is invalid")
	}
	key := keys[value.KeyID]
	detached, err := base64.StdEncoding.Strict().DecodeString(value.Value)
	if err != nil || len(detached) != ed25519.SignatureSize || key == nil || !ed25519.Verify(key, payload, detached) {
		return fmt.Errorf("signature is not trusted")
	}
	return nil
}

func requiredArtifacts(value *manifest, platform, architecture string) ([]artifact, error) {
	target := platform + "-" + architecture
	ids := []string{"core-" + target}
	for _, packID := range []string{"browser", "channels", "image", "ocr", "office", "sandbox"} {
		base := "capability-pack-" + packID + "-" + target
		ids = append(ids, base, base+"-manifest")
	}
	required := make(map[string]bool, len(ids))
	for _, artifactID := range ids {
		required[artifactID] = true
	}
	byID := make(map[string]artifact, len(value.Artifacts))
	for _, item := range value.Artifacts {
		byID[item.ArtifactID] = item
		if item.Platform == platform && item.Architecture == architecture &&
			strings.HasPrefix(item.ArtifactID, "capability-pack-") && !required[item.ArtifactID] {
			return nil, fmt.Errorf("release contains an unexpected host Capability Pack")
		}
	}
	result := make([]artifact, 0, len(ids))
	for _, artifactID := range ids {
		item, ok := byID[artifactID]
		if !ok || item.Platform != platform || item.Architecture != architecture {
			return nil, fmt.Errorf("release is missing the required Core or Capability Pack")
		}
		result = append(result, item)
	}
	return result, nil
}

func downloadArtifact(ctx context.Context, client *http.Client, release *manifest, item artifact, destination string, keys map[string]ed25519.PublicKey) error {
	if fileMatches(destination, item.SizeBytes, item.SHA256) {
		return verifyArtifactSignature(release, item, keys)
	}
	_ = os.Remove(destination)
	var last error
	for _, origin := range release.Sources {
		partial := destination + ".partial-" + origin.SourceID
		location := strings.TrimRight(origin.BaseURL, "/") + "/" + url.PathEscape(item.FileName)
		if err := downloadFromSource(ctx, client, location, partial, item.SizeBytes); err != nil {
			last = err
			_ = os.Remove(partial)
			continue
		}
		if !fileMatches(partial, item.SizeBytes, item.SHA256) || verifyArtifactSignature(release, item, keys) != nil {
			last = fmt.Errorf("artifact verification failed")
			_ = os.Remove(partial)
			continue
		}
		if err := os.Rename(partial, destination); err != nil {
			return fmt.Errorf("verified artifact could not be committed")
		}
		return nil
	}
	return fmt.Errorf("all signed artifact sources failed: %w", last)
}

func downloadFromSource(ctx context.Context, client *http.Client, location, destination string, expected int64) error {
	if err := validateHTTPS(location); err != nil {
		return err
	}
	resume := int64(0)
	if metadata, err := os.Stat(destination); err == nil {
		if !metadata.Mode().IsRegular() || metadata.Size() > expected {
			return fmt.Errorf("partial artifact is unsafe")
		}
		resume = metadata.Size()
	}
	if resume == expected {
		return nil
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, location, nil)
	if err != nil {
		return err
	}
	request.Header.Set("Accept-Encoding", "identity")
	if resume > 0 {
		request.Header.Set("Range", "bytes="+strconv.FormatInt(resume, 10)+"-")
	}
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	expectedStatus := http.StatusOK
	if resume > 0 {
		expectedStatus = http.StatusPartialContent
		expectedRange := fmt.Sprintf("bytes %d-%d/%d", resume, expected-1, expected)
		if response.Header.Get("Content-Range") != expectedRange {
			return fmt.Errorf("release source did not honor the resume range")
		}
	}
	if response.StatusCode != expectedStatus || response.Header.Get("Content-Encoding") != "" && !strings.EqualFold(response.Header.Get("Content-Encoding"), "identity") || response.ContentLength >= 0 && response.ContentLength != expected-resume {
		return fmt.Errorf("release source returned an invalid bounded response")
	}
	flags := os.O_CREATE | os.O_WRONLY
	if resume > 0 {
		flags |= os.O_APPEND
	} else {
		flags |= os.O_EXCL
	}
	file, err := os.OpenFile(destination, flags, 0o600)
	if err != nil {
		return err
	}
	written, copyErr := io.CopyN(file, response.Body, expected-resume)
	extra := make([]byte, 1)
	extraCount, extraErr := response.Body.Read(extra)
	syncErr := file.Sync()
	closeErr := file.Close()
	if copyErr != nil || written != expected-resume || extraCount != 0 || extraErr != io.EOF || syncErr != nil || closeErr != nil {
		return fmt.Errorf("release source ended outside the signed artifact bound")
	}
	return nil
}

func verifyArtifactSignature(release *manifest, item artifact, keys map[string]ed25519.PublicKey) error {
	payload := strings.Join([]string{"ecorex-artifact-v1", release.ReleaseID, release.Version, release.BuildDigest, item.ArtifactID, item.Platform, item.Architecture, item.FileName, strconv.FormatInt(item.SizeBytes, 10), item.SHA256, ""}, "\n")
	return verifySignature([]byte(payload), item.Signature, keys)
}

func fileMatches(fileName string, expectedSize int64, expectedDigest string) bool {
	metadata, err := os.Lstat(fileName)
	if err != nil || !metadata.Mode().IsRegular() || metadata.Size() != expectedSize {
		return false
	}
	file, err := os.Open(fileName)
	if err != nil {
		return false
	}
	defer file.Close()
	digest := sha256.New()
	if _, err := io.Copy(digest, file); err != nil {
		return false
	}
	return hex.EncodeToString(digest.Sum(nil)) == expectedDigest
}

func sha256Hex(payload []byte) string {
	digest := sha256.Sum256(payload)
	return hex.EncodeToString(digest[:])
}

func extractCore(archivePath, destination string) error {
	reader, err := zip.OpenReader(archivePath)
	if err != nil {
		return fmt.Errorf("verified Core archive is unreadable")
	}
	defer reader.Close()
	if len(reader.File) == 0 || len(reader.File) > maxFiles {
		return fmt.Errorf("Core archive entry count is invalid")
	}
	if err := os.MkdirAll(destination, 0o700); err != nil {
		return err
	}
	seen := map[string]bool{}
	var total int64
	for _, entry := range reader.File {
		name := strings.ReplaceAll(entry.Name, "\\", "/")
		clean := path.Clean(name)
		if name == "" || strings.HasPrefix(name, "/") || clean == "." || clean == ".." || strings.HasPrefix(clean, "../") || strings.Contains(name, ":") || clean != strings.TrimSuffix(name, "/") && !entry.FileInfo().IsDir() {
			return fmt.Errorf("Core archive contains an unsafe path")
		}
		collision := strings.ToLower(clean)
		if seen[collision] {
			return fmt.Errorf("Core archive contains a path collision")
		}
		seen[collision] = true
		mode := entry.Mode()
		if mode&os.ModeSymlink != 0 || mode&os.ModeType != 0 && !entry.FileInfo().IsDir() {
			return fmt.Errorf("Core archive contains a special file")
		}
		target := filepath.Join(destination, filepath.FromSlash(clean))
		relative, err := filepath.Rel(destination, target)
		if err != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
			return fmt.Errorf("Core archive escapes its destination")
		}
		if entry.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
			continue
		}
		total += int64(entry.UncompressedSize64)
		if total > maxCoreBytes {
			return fmt.Errorf("Core archive expands beyond its bound")
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}
		input, err := entry.Open()
		if err != nil {
			return err
		}
		permissions := os.FileMode(0o644)
		if mode&0o111 != 0 {
			permissions = 0o755
		}
		output, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, permissions)
		if err != nil {
			input.Close()
			return err
		}
		written, copyErr := io.CopyN(output, input, int64(entry.UncompressedSize64))
		extra := make([]byte, 1)
		extraCount, extraErr := input.Read(extra)
		syncErr := output.Sync()
		closeErr := output.Close()
		input.Close()
		if copyErr != nil && !errors.Is(copyErr, io.EOF) || written != int64(entry.UncompressedSize64) || extraCount != 0 || extraErr != io.EOF || syncErr != nil || closeErr != nil {
			return fmt.Errorf("Core archive entry is incomplete")
		}
	}
	return nil
}

func persistTrust(root string, encoded map[string]string, keys map[string]ed25519.PublicKey) ([]string, error) {
	directory := filepath.Join(root, "trust")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return nil, fmt.Errorf("release trust directory is unavailable")
	}
	ids := make([]string, 0, len(keys))
	for keyID := range keys {
		ids = append(ids, keyID)
	}
	sort.Strings(ids)
	definitions := make([]string, 0, len(ids))
	for _, keyID := range ids {
		if base64.StdEncoding.EncodeToString(keys[keyID]) != encoded[keyID] {
			return nil, fmt.Errorf("release trust key changed")
		}
		fileName := filepath.Join(directory, keyID+".ed25519.pub")
		if err := atomicWrite(fileName, keys[keyID], 0o600); err != nil {
			return nil, fmt.Errorf("release trust key could not be persisted")
		}
		definitions = append(definitions, keyID+"="+fileName)
	}
	return definitions, nil
}

func installLocal(coreRoot, root, manifestPath, artifactsDir string, trusted []string, sandboxHelper string, sandboxHelperSHA256 string) (installResult, error) {
	python := filepath.Join(coreRoot, "bin", "pack-python", "bin", "python3")
	if runtime.GOOS == "windows" {
		python = filepath.Join(coreRoot, "bin", "pack-python", "python.exe")
	}
	arguments := []string{"-I", "-B", "-m", "ecorex.bootstrap.install_local", "--manifest", manifestPath, "--artifacts", artifactsDir, "--install-root", root}
	if runtime.GOOS == "windows" {
		arguments = append(arguments, "--sandbox-helper", sandboxHelper, "--sandbox-helper-sha256", sandboxHelperSHA256)
	}
	for _, definition := range trusted {
		arguments = append(arguments, "--trusted-public-key", definition)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	command := exec.CommandContext(ctx, python, arguments...)
	command.Dir = coreRoot
	command.Env = minimalEnvironment()
	stdout := boundedBuffer{limit: 64 * 1024}
	stderr := boundedBuffer{limit: 16 * 1024}
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Run(); err != nil || ctx.Err() != nil || stdout.overflow || stderr.overflow || len(stdout.Bytes()) == 0 {
		return installResult{}, fmt.Errorf("verified Runtime could not stage the first install")
	}
	var result installResult
	if err := decodeExact(stdout.Bytes(), &result); err != nil || result.SchemaVersion != 1 || result.State != "healthchecking" && result.State != "completed" || !safeID.MatchString(result.TransactionID) || !safeID.MatchString(result.SlotID) {
		return installResult{}, fmt.Errorf("first-install result is invalid")
	}
	return result, nil
}

func supervise(python, root string, trusted []string, legacySource string) error {
	arguments := []string{"-I", "-B", "-m", "ecorex.bootstrap", "--install-root", root}
	if legacySource != "" {
		arguments = append(arguments, "--legacy-v030-source", legacySource)
	}
	for _, definition := range trusted {
		arguments = append(arguments, "--trusted-public-key", definition)
	}
	command := exec.Command(python, arguments...)
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	command.Stdin = os.Stdin
	command.Env = minimalEnvironment()
	if err := command.Run(); err != nil {
		return fmt.Errorf("installed Runtime did not pass Bootstrap health")
	}
	return nil
}

func minimalEnvironment() []string {
	allowed := map[string]bool{"APPDATA": true, "HOME": true, "LANG": true, "LC_ALL": true, "LOCALAPPDATA": true, "SYSTEMDRIVE": true, "SYSTEMROOT": true, "TEMP": true, "TMP": true, "USERPROFILE": true, "WINDIR": true}
	result := []string{"PYTHONDONTWRITEBYTECODE=1", "PYTHONNOUSERSITE=1", "PYTHONUTF8=1"}
	for _, item := range os.Environ() {
		name, _, _ := strings.Cut(item, "=")
		if allowed[strings.ToUpper(name)] {
			result = append(result, item)
		}
	}
	return result
}

func atomicWrite(fileName string, payload []byte, permissions os.FileMode) error {
	if existing, err := os.ReadFile(fileName); err == nil && bytes.Equal(existing, payload) {
		return nil
	}
	temporary := fileName + ".tmp-" + strconv.FormatInt(time.Now().UnixNano(), 36)
	file, err := os.OpenFile(temporary, os.O_CREATE|os.O_EXCL|os.O_WRONLY, permissions)
	if err != nil {
		return err
	}
	_, writeErr := file.Write(payload)
	syncErr := file.Sync()
	closeErr := file.Close()
	if writeErr != nil || syncErr != nil || closeErr != nil {
		_ = os.Remove(temporary)
		return fmt.Errorf("atomic file write failed")
	}
	if err := replaceFileAtomically(temporary, fileName); err != nil {
		_ = os.Remove(temporary)
		return err
	}
	return nil
}

func validateHTTPS(value string) error {
	parsed, err := url.Parse(value)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" || parsed.Port() != "" && parsed.Port() != "443" {
		return fmt.Errorf("URL is not an allowed HTTPS endpoint")
	}
	return nil
}

func safeFileName(value string) bool {
	return value != "" && filepath.Base(value) == value && !strings.ContainsAny(value, "/\\:\x00\r\n") && value != "." && value != ".."
}

func decodeExact(payload []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if decoder.Decode(&struct{}{}) != io.EOF {
		return fmt.Errorf("JSON document has trailing content")
	}
	return nil
}

func writeJSON(value any) {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(value)
}

func fail(_ error) {
	fmt.Fprintln(os.Stderr, "EcoreX Bootstrap stopped safely.")
	os.Exit(1)
}
