package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
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
	maxIndexBytes      = 256 * 1024
	maxManifestBytes   = 1024 * 1024
	maxBootstrapBytes  = 10 * 1024 * 1024
	maxCoreBytes       = 150 * 1024 * 1024
	maxPackBytes       = 500 * 1024 * 1024
	maxFiles           = 50_000
	artifactChunkBytes = 8 * 1024 * 1024
	productWebUIURL    = "http://127.0.0.1:8765/"
)

var (
	safeID              = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`)
	releaseIDPattern    = regexp.MustCompile(`^release-stable-[0-9a-f]{24}$`)
	sha256Pattern       = regexp.MustCompile(`^[0-9a-f]{64}$`)
	stableSemverPattern = regexp.MustCompile(`^(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$`)
	semverPattern       = regexp.MustCompile(`^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$`)
	errProductLocked    = errors.New("another EcoreX install or Runtime is active")
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
	SchemaVersion         int    `json:"schema_version"`
	LegacyV030Source      string `json:"legacy_v030_source"`
	LegacySource          string `json:"legacy_source,omitempty"`
	LegacySourceVersion   string `json:"legacy_source_version,omitempty"`
	LegacyReleaseEvidence string `json:"legacy_release_evidence,omitempty"`
}

type legacySelection struct {
	Source          string
	SourceVersion   string
	ReleaseEvidence string
}

type legacyRuntimeManifest struct {
	SchemaVersion string `json:"schemaVersion"`
	Product       string `json:"product"`
	Version       string `json:"version"`
	ReleaseGate   struct {
		InstallReady bool `json:"installReady"`
	} `json:"releaseGate"`
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

type runtimeOwnerReceipt struct {
	SchemaVersion int    `json:"schema_version"`
	Nonce         string `json:"nonce"`
	IssuedAt      string `json:"issued_at"`
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
	localRelease := flag.String("local-release", "", "install an authenticated local release directory")
	installRootFlag := flag.String("install-root", "", "override the EcoreX data root")
	launchInstalled := flag.Bool(
		"launch-installed",
		false,
		"launch the already-installed signed Runtime without release discovery",
	)
	flag.Parse()
	if *selfTest {
		platform, architecture, err := productTarget()
		if err != nil {
			fail(err)
		}
		writeJSON(map[string]any{"schema_version": 1, "status": "passed", "platform": platform, "architecture": architecture})
		return
	}
	if *launchInstalled {
		if *indexURL != "" || *localRelease != "" {
			fail(fmt.Errorf("installed Runtime launch does not accept release discovery overrides"))
		}
		if err := runInstalled(*installRootFlag); err != nil {
			fail(err)
		}
		return
	}
	if *localRelease != "" {
		if *indexURL != "" {
			fail(fmt.Errorf("local release install does not accept public discovery overrides"))
		}
		if err := runLocalRelease(*localRelease, *installRootFlag); err != nil {
			fail(err)
		}
		return
	}
	if err := run(*indexURL, *installRootFlag); err != nil {
		fail(err)
	}
}

func runLocalRelease(localRelease, rootOverride string) error {
	progress := newBootstrapProgress(os.Stderr)
	progress.Stage("准备", "正在验证本地 e-Mate WebUI 发布包")
	configuration, _, keys, _, err := loadConfig()
	if err != nil {
		return err
	}
	releaseDir, _, manifestBytes, release, selected, err := loadLocalRelease(localRelease, keys, *configuration.MinimumStable)
	if err != nil {
		return err
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
		if !errors.Is(err, errProductLocked) {
			return err
		}
		progress.Stage("等待", "正在等待旧版 e-Mate 安全退出")
		lock, err = acquireLocalInstallLock(
			filepath.Join(root, "bootstrap-launch.lock"),
			5*time.Minute,
			250*time.Millisecond,
		)
		if err != nil {
			return err
		}
	}
	defer lock.close()
	if err := ensureBootstrapStateDirectory(root); err != nil {
		return err
	}
	if err := ensureRuntimeDataDirectories(root); err != nil {
		return err
	}
	bootstrapHelper, err := stageSandboxHelper(root, configuration.SandboxHelperSHA256)
	if err != nil {
		return err
	}
	legacy, err := selectLegacyMigration(root)
	if err != nil {
		return err
	}
	work := filepath.Join(root, "bootstrap-work", release.ReleaseID)
	if err := os.MkdirAll(work, 0o700); err != nil {
		return fmt.Errorf("bootstrap workspace is unavailable")
	}
	artifactsDir := filepath.Join(work, "artifacts")
	_ = os.RemoveAll(artifactsDir)
	if err := os.MkdirAll(artifactsDir, 0o700); err != nil {
		return fmt.Errorf("bootstrap artifact directory is unavailable")
	}
	for _, item := range selected {
		if err := copyLocalArtifact(filepath.Join(releaseDir, item.FileName), filepath.Join(artifactsDir, item.FileName), item); err != nil {
			return err
		}
	}
	stagedManifest := filepath.Join(artifactsDir, "release-manifest.json")
	if err := atomicWrite(stagedManifest, manifestBytes, 0o600); err != nil {
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
	result, err := installLocal(coreRoot, root, stagedManifest, artifactsDir, trustedDefinitions, bootstrapHelper, configuration.SandboxHelperSHA256)
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
	ownerNonce, err := issueRuntimeOwnerReceipt(root)
	if err != nil {
		return err
	}
	go func() {
		_ = waitForRuntimeAndOpen(root, 5*time.Minute)
	}()
	return supervise(python, root, trustedDefinitions, legacy, ownerNonce)
}

func acquireLocalInstallLock(path string, timeout, poll time.Duration) (*productLock, error) {
	if timeout <= 0 || timeout > 15*time.Minute || poll <= 0 || poll > timeout {
		return nil, fmt.Errorf("local install lock wait is invalid")
	}
	deadline := time.Now().Add(timeout)
	for {
		lock, err := acquireProductLock(path)
		if err == nil {
			return lock, nil
		}
		if !errors.Is(err, errProductLocked) {
			return nil, err
		}
		if !time.Now().Before(deadline) {
			return nil, fmt.Errorf("old Runtime did not exit for local install")
		}
		time.Sleep(min(poll, time.Until(deadline)))
	}
}

func loadLocalRelease(localRelease string, keys map[string]ed25519.PublicKey, floor minimumStable) (string, string, []byte, manifest, []artifact, error) {
	if !filepath.IsAbs(localRelease) || strings.ContainsAny(localRelease, "\x00\r\n") {
		return "", "", nil, manifest{}, nil, fmt.Errorf("local release directory must be absolute")
	}
	releaseDir := filepath.Clean(localRelease)
	metadata, statErr := os.Lstat(releaseDir)
	resolved, resolveErr := filepath.EvalSymlinks(releaseDir)
	if statErr != nil || !metadata.IsDir() || metadata.Mode()&os.ModeSymlink != 0 || resolveErr != nil || !samePath(releaseDir, resolved) {
		return "", "", nil, manifest{}, nil, fmt.Errorf("local release directory is unsafe")
	}
	manifestPath := filepath.Join(releaseDir, "release-manifest.json")
	manifestBytes, err := readStableRegularFile(manifestPath, maxManifestBytes)
	if err != nil {
		return "", "", nil, manifest{}, nil, fmt.Errorf("local release manifest is unavailable")
	}
	var release manifest
	if err := decodeExact(manifestBytes, &release); err != nil {
		return "", "", nil, manifest{}, nil, fmt.Errorf("local release manifest is invalid")
	}
	discovery := localManifestDiscovery(&release)
	if err := validateManifest(&release, &discovery, keys); err != nil {
		return "", "", nil, manifest{}, nil, err
	}
	sequence, err := stableReleaseSequence(release.Version)
	if err != nil || validateMinimumStable(floor, keys) != nil || sequence < floor.Sequence {
		return "", "", nil, manifest{}, nil, fmt.Errorf("local release is below the Bootstrap minimum stable target")
	}
	platform, architecture, err := productTarget()
	if err != nil {
		return "", "", nil, manifest{}, nil, err
	}
	selected, err := requiredArtifacts(&release, platform, architecture)
	if err != nil {
		return "", "", nil, manifest{}, nil, err
	}
	byID := make(map[string]artifact, len(release.Artifacts))
	for _, item := range release.Artifacts {
		byID[item.ArtifactID] = item
	}
	for _, artifactID := range []string{"bootstrap-" + platform + "-" + architecture, "web-manifest"} {
		item, ok := byID[artifactID]
		if !ok || artifactID == "web-manifest" && (item.Platform != "all" || item.Architecture != "all") || artifactID != "web-manifest" && (item.Platform != platform || item.Architecture != architecture || item.SizeBytes > maxBootstrapBytes) {
			return "", "", nil, manifest{}, nil, fmt.Errorf("local release is missing a required signed artifact")
		}
		selected = append(selected, item)
	}
	required := map[string]bool{}
	for _, item := range selected {
		required[item.FileName] = true
		path := filepath.Join(releaseDir, item.FileName)
		if !fileMatches(path, item.SizeBytes, item.SHA256) || verifyArtifactSignature(&release, item, keys) != nil {
			return "", "", nil, manifest{}, nil, fmt.Errorf("local release artifact verification failed")
		}
	}
	byFileName := make(map[string]artifact, len(release.Artifacts))
	for _, item := range release.Artifacts {
		if _, duplicate := byFileName[item.FileName]; duplicate {
			return "", "", nil, manifest{}, nil, fmt.Errorf("local release manifest repeats an artifact file name")
		}
		byFileName[item.FileName] = item
	}
	entries, err := os.ReadDir(releaseDir)
	if err != nil {
		return "", "", nil, manifest{}, nil, fmt.Errorf("local release directory inventory is invalid")
	}
	observed := map[string]bool{}
	for _, entry := range entries {
		if entry.Name() == "release-manifest.json" && !entry.IsDir() {
			continue
		}
		item, signed := byFileName[entry.Name()]
		path := filepath.Join(releaseDir, entry.Name())
		if entry.IsDir() || !signed || observed[entry.Name()] || !fileMatches(path, item.SizeBytes, item.SHA256) || verifyArtifactSignature(&release, item, keys) != nil {
			return "", "", nil, manifest{}, nil, fmt.Errorf("local release directory inventory is invalid")
		}
		observed[entry.Name()] = true
	}
	for fileName := range required {
		if !observed[fileName] {
			return "", "", nil, manifest{}, nil, fmt.Errorf("local release is missing a required signed artifact")
		}
	}
	return releaseDir, manifestPath, manifestBytes, release, selected, nil
}

func localManifestDiscovery(value *manifest) indexRelease {
	sources := make([]indexSource, len(value.Sources))
	for position, item := range value.Sources {
		sources[position] = indexSource{SourceID: item.SourceID, Kind: item.Kind, Priority: item.Priority, URL: strings.TrimRight(item.BaseURL, "/") + "/release-manifest.json"}
	}
	return indexRelease{
		ReleaseID: value.ReleaseID,
		Version: value.Version,
		Channel: value.Channel,
		BuildDigest: value.BuildDigest,
		Manifest: indexManifest{Signature: value.Signature, Sources: sources},
	}
}

func copyLocalArtifact(source, destination string, item artifact) error {
	input, err := os.Open(source)
	if err != nil {
		return fmt.Errorf("local release artifact is unavailable")
	}
	defer input.Close()
	output, err := os.OpenFile(destination, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("local release artifact could not be staged")
	}
	written, copyErr := io.Copy(output, input)
	syncErr := output.Sync()
	closeErr := output.Close()
	if copyErr != nil || syncErr != nil || closeErr != nil || written != item.SizeBytes || !fileMatches(destination, item.SizeBytes, item.SHA256) {
		_ = os.Remove(destination)
		return fmt.Errorf("local release artifact changed while staging")
	}
	return nil
}

func runInstalled(rootOverride string) error {
	progress := newBootstrapProgress(os.Stderr)
	progress.Stage("启动", "正在检查本机 EcoreX")
	configuration, _, keys, _, err := loadConfig()
	if err != nil {
		return err
	}
	root, err := installRoot(rootOverride)
	if err != nil {
		return err
	}
	lock, err := acquireProductLock(filepath.Join(root, "bootstrap-launch.lock"))
	if err != nil {
		if errors.Is(err, errProductLocked) {
			progress.Stage("等待", "EcoreX 正在启动，准备好后会自动打开浏览器")
			return waitForRuntimeAndOpen(root, 5*time.Minute)
		}
		return err
	}
	defer lock.close()
	if err := ensureBootstrapStateDirectory(root); err != nil {
		return err
	}
	if err := ensureRuntimeDataDirectories(root); err != nil {
		return err
	}
	if opened, err := openRunningRuntime(root); err != nil {
		return err
	} else if opened {
		progress.Success("EcoreX 已在运行，浏览器已打开")
		return nil
	}
	progress.Stage("准备", "正在确认已安装版本与本机安全组件")
	if _, err := stageSandboxHelper(root, configuration.SandboxHelperSHA256); err != nil {
		return err
	}
	legacy, err := selectLegacyMigration(root)
	if err != nil {
		return err
	}
	trustedDefinitions, err := persistTrust(
		root, configuration.ReleasePublicKeys, keys,
	)
	if err != nil {
		return err
	}
	python, err := installedPython(root)
	if err != nil {
		return err
	}
	ownerNonce, err := issueRuntimeOwnerReceipt(root)
	if err != nil {
		return err
	}
	progress.Stage("启动", "正在启动本地服务，准备好后会自动打开浏览器")
	go func() {
		if waitForRuntimeAndOpen(root, 5*time.Minute) == nil {
			progress.Success("EcoreX 已就绪，浏览器已打开")
		}
	}()
	return supervise(python, root, trustedDefinitions, legacy, ownerNonce)
}

func run(indexOverride, rootOverride string) error {
	progress := newBootstrapProgress(os.Stderr)
	progress.Stage("准备", "正在检查系统、存储空间与安装状态")
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
		if errors.Is(err, errProductLocked) {
			progress.Stage("等待", "另一个 EcoreX 进程正在工作，准备好后会自动打开浏览器")
			return waitForRuntimeAndOpen(root, 5*time.Minute)
		}
		return err
	}
	defer lock.close()
	if err := ensureBootstrapStateDirectory(root); err != nil {
		return err
	}
	if err := ensureRuntimeDataDirectories(root); err != nil {
		return err
	}
	if opened, err := openRunningRuntime(root); err != nil {
		return err
	} else if opened {
		progress.Success("EcoreX 已在运行，浏览器已打开")
		return nil
	}
	progress.Stage("准备", "正在准备本机安全组件")
	bootstrapHelper, err := stageSandboxHelper(root, configuration.SandboxHelperSHA256)
	if err != nil {
		return err
	}
	legacy, err := selectLegacyMigration(root)
	if err != nil {
		return err
	}

	client := newHTTPClient()
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Minute)
	defer cancel()
	progress.Stage("连接", "正在读取正式版发布信息")
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
	progress.Stage("版本", fmt.Sprintf("已确认 EcoreX v%s，正在准备所需组件", release.Version))
	platform, architecture, err := productTarget()
	if err != nil {
		return err
	}
	selected, err := requiredArtifacts(&release, platform, architecture)
	if err != nil {
		return err
	}
	bootstrapArtifact, err := requiredBootstrapArtifact(
		&release, index.Release, platform, architecture,
	)
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
	artifactCount := len(selected) + 1
	for index, item := range selected {
		progress.BeginArtifact(item, index+1, artifactCount)
		destination := filepath.Join(artifactsDir, item.FileName)
		if err := downloadArtifact(
			ctx,
			client,
			&release,
			item,
			destination,
			keys,
			progress,
		); err != nil {
			return err
		}
	}
	progress.BeginArtifact(bootstrapArtifact, artifactCount, artifactCount)
	bootstrapArchive := filepath.Join(artifactsDir, bootstrapArtifact.FileName)
	if err := downloadArtifact(
		ctx,
		client,
		&release,
		bootstrapArtifact,
		bootstrapArchive,
		keys,
		progress,
	); err != nil {
		return err
	}
	manifestPath := filepath.Join(artifactsDir, "release-manifest.json")
	if err := atomicWrite(manifestPath, manifestBytes, 0o600); err != nil {
		return fmt.Errorf("release manifest could not be staged")
	}
	core := selected[0]
	coreRoot := filepath.Join(work, "core")
	_ = os.RemoveAll(coreRoot)
	extractActivity := progress.BeginActivity(
		"解压",
		"正在展开已验证的 EcoreX 核心",
	)
	extractErr := extractCore(
		filepath.Join(artifactsDir, core.FileName),
		coreRoot,
	)
	extractActivity.End()
	if extractErr != nil {
		return extractErr
	}
	progress.Stage("解压", "EcoreX 核心已准备完成")
	trustedDefinitions, err := persistTrust(root, configuration.ReleasePublicKeys, keys)
	if err != nil {
		return err
	}
	installActivity := progress.BeginActivity(
		"安装",
		"正在写入本机版本、迁移数据并创建快捷入口",
	)
	result, err := installLocal(
		coreRoot,
		root,
		manifestPath,
		artifactsDir,
		trustedDefinitions,
		bootstrapHelper,
		configuration.SandboxHelperSHA256,
	)
	installActivity.End()
	if err != nil {
		return err
	}
	progress.Stage("安装", "本机版本与快捷入口已安装完成")
	installedPayload := filepath.Join(root, "slots", result.SlotID, "payload")
	python := filepath.Join(installedPayload, "bin", "pack-python", "bin", "python3")
	if runtime.GOOS == "windows" {
		python = filepath.Join(installedPayload, "bin", "pack-python", "python.exe")
	}
	if err := os.RemoveAll(work); err != nil {
		return fmt.Errorf("bootstrap workspace cleanup failed")
	}
	ownerNonce, err := issueRuntimeOwnerReceipt(root)
	if err != nil {
		return err
	}
	progress.Stage("启动", "正在启动本地服务，准备好后会自动打开浏览器")
	go func() {
		if waitForRuntimeAndOpen(root, 5*time.Minute) == nil {
			progress.Success("EcoreX 已就绪，浏览器已打开；以后可从桌面快捷方式再次启动")
		}
	}()
	return supervise(python, root, trustedDefinitions, legacy, ownerNonce)
}

func waitForRuntimeAndOpen(root string, timeout time.Duration) error {
	if timeout <= 0 || timeout > 15*time.Minute {
		return fmt.Errorf("Runtime launch wait is invalid")
	}
	deadline := time.Now().Add(timeout)
	client := &http.Client{
		Timeout: 3 * time.Second,
		Transport: &http.Transport{
			Proxy: nil,
			DialContext: (&net.Dialer{
				Timeout:   2 * time.Second,
				KeepAlive: -1,
			}).DialContext,
			DisableKeepAlives: true,
		},
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	defer client.CloseIdleConnections()
	for {
		if runtimeUIReadyAt(client, root, productWebUIURL) {
			if err := openWebUI(productWebUIURL); err != nil {
				return fmt.Errorf("EcoreX WebUI could not be opened")
			}
			if err := recordBrowserOpen(root, productWebUIURL); err != nil {
				return err
			}
			return nil
		}
		if !time.Now().Before(deadline) {
			return fmt.Errorf("EcoreX Runtime did not become ready")
		}
		time.Sleep(250 * time.Millisecond)
	}
}

func openRunningRuntime(root string) (bool, error) {
	return openRunningRuntimeAt(root, productWebUIURL, openWebUI)
}

func openRunningRuntimeAt(
	root, webUIURL string,
	opener func(string) error,
) (bool, error) {
	if opener == nil {
		return false, fmt.Errorf("WebUI opener is unavailable")
	}
	client := &http.Client{
		Timeout: 3 * time.Second,
		Transport: &http.Transport{
			Proxy: nil,
			DialContext: (&net.Dialer{
				Timeout:   2 * time.Second,
				KeepAlive: -1,
			}).DialContext,
			DisableKeepAlives: true,
		},
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	defer client.CloseIdleConnections()
	if !runtimeUIReadyAt(client, root, webUIURL) {
		return false, nil
	}
	if err := opener(webUIURL); err != nil {
		return false, fmt.Errorf("EcoreX WebUI could not be opened")
	}
	if err := recordBrowserOpen(root, webUIURL); err != nil {
		return false, err
	}
	return true, nil
}

func recordBrowserOpen(root, webUIURL string) error {
	releaseID, version, ok := installedRuntimeIdentity(root)
	if !ok || webUIURL == "" {
		return fmt.Errorf("WebUI browser receipt identity is unavailable")
	}
	payload, err := json.Marshal(map[string]any{
		"schema_version": 1,
		"status":         "opened",
		"release_id":     releaseID,
		"version":        version,
		"url":            webUIURL,
		"opened_at":      time.Now().UTC().Truncate(time.Second).Format("2006-01-02T15:04:05Z"),
	})
	if err != nil {
		return fmt.Errorf("WebUI browser receipt could not be encoded")
	}
	if err := atomicWrite(
		filepath.Join(root, "bootstrap", "browser-opened.json"),
		append(payload, '\n'),
		0o600,
	); err != nil {
		return fmt.Errorf("WebUI browser receipt could not be persisted")
	}
	return nil
}

func runtimeUIReadyAt(client *http.Client, root, webUIURL string) bool {
	if client == nil || webUIURL == "" {
		return false
	}
	ownerNonce, ok := readRuntimeOwnerReceipt(root)
	if !ok {
		return false
	}
	releaseID, version, ok := installedRuntimeIdentity(root)
	if !ok {
		return false
	}
	ownerRequest, err := http.NewRequest(
		http.MethodGet,
		strings.TrimRight(webUIURL, "/")+"/api/v1/runtime-owner",
		nil,
	)
	if err != nil {
		return false
	}
	ownerRequest.Header.Set("X-EcoreX-Owner-Nonce", ownerNonce)
	ownerResponse, err := client.Do(ownerRequest)
	if err != nil {
		return false
	}
	ownerResponse.Body.Close()
	if ownerResponse.StatusCode != http.StatusNoContent ||
		ownerResponse.Header.Get("X-EcoreX-Runtime-Owner") != "verified" ||
		ownerResponse.Header.Get("Cache-Control") != "no-store" {
		return false
	}
	request, err := http.NewRequest(http.MethodGet, webUIURL, nil)
	if err != nil {
		return false
	}
	response, err := client.Do(request)
	if err != nil {
		return false
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK || response.Header.Get("Cache-Control") != "no-store" {
		return false
	}
	payload, err := io.ReadAll(io.LimitReader(response.Body, 1024*1024+1))
	if err != nil || len(payload) == 0 || len(payload) > 1024*1024 {
		return false
	}
	return bytes.Contains(payload, []byte("window.__ECOREX_RUNTIME__=Object.freeze(")) &&
		bytes.Contains(payload, []byte(`"releaseId":"`+releaseID+`"`)) &&
		bytes.Contains(payload, []byte(`"version":"`+version+`"`))
}

func issueRuntimeOwnerReceipt(root string) (string, error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", fmt.Errorf("Runtime owner nonce could not be generated")
	}
	nonce := base64.RawURLEncoding.EncodeToString(raw)
	receipt := runtimeOwnerReceipt{
		SchemaVersion: 1,
		Nonce:         nonce,
		IssuedAt:      time.Now().UTC().Truncate(time.Second).Format("2006-01-02T15:04:05Z"),
	}
	payload, err := json.Marshal(receipt)
	if err != nil {
		return "", fmt.Errorf("Runtime owner receipt could not be encoded")
	}
	payload = append(payload, '\n')
	path := filepath.Join(root, "bootstrap", "runtime-owner.json")
	if err := atomicWrite(path, payload, 0o600); err != nil {
		return "", fmt.Errorf("Runtime owner receipt could not be persisted")
	}
	return nonce, nil
}

func readRuntimeOwnerReceipt(root string) (string, bool) {
	path := filepath.Join(root, "bootstrap", "runtime-owner.json")
	info, err := os.Lstat(path)
	if err != nil ||
		!info.Mode().IsRegular() ||
		info.Mode()&os.ModeSymlink != 0 ||
		info.Size() < 1 ||
		info.Size() > 1024 {
		return "", false
	}
	payload, err := os.ReadFile(path)
	if err != nil || int64(len(payload)) != info.Size() {
		return "", false
	}
	var receipt runtimeOwnerReceipt
	if decodeExact(payload, &receipt) != nil ||
		receipt.SchemaVersion != 1 ||
		len(receipt.Nonce) != 43 {
		return "", false
	}
	decoded, err := base64.RawURLEncoding.Strict().DecodeString(receipt.Nonce)
	if err != nil || len(decoded) != 32 {
		return "", false
	}
	if _, err := parseCanonicalUTCSecond(receipt.IssuedAt); err != nil {
		return "", false
	}
	return receipt.Nonce, true
}

func installedRuntimeIdentity(root string) (string, string, bool) {
	pointers, ok := readInstalledPointers(root)
	if !ok {
		return "", "", false
	}
	markerBytes, err := os.ReadFile(filepath.Join(root, "slots", pointers.Current, ".slot.json"))
	if err != nil || len(markerBytes) == 0 || len(markerBytes) > 64*1024 {
		return "", "", false
	}
	var marker map[string]any
	if err := json.Unmarshal(markerBytes, &marker); err != nil {
		return "", "", false
	}
	releaseID, releaseOK := marker["release_id"].(string)
	version, versionOK := marker["version"].(string)
	if !releaseOK || !versionOK || !releaseIDPattern.MatchString(releaseID) || !semverPattern.MatchString(version) {
		return "", "", false
	}
	return releaseID, version, true
}

type installedPointers struct {
	Current   string   `json:"current"`
	Previous  *string  `json:"previous"`
	KnownGood []string `json:"known_good"`
}

func readInstalledPointers(root string) (installedPointers, bool) {
	pointerBytes, err := os.ReadFile(filepath.Join(root, "slot-pointers.json"))
	if err != nil || len(pointerBytes) == 0 || len(pointerBytes) > 16*1024 {
		return installedPointers{}, false
	}
	var pointers installedPointers
	if err := decodeExact(pointerBytes, &pointers); err != nil ||
		!safeID.MatchString(pointers.Current) ||
		!slicesContain(pointers.KnownGood, pointers.Current) {
		return installedPointers{}, false
	}
	if pointers.Previous != nil && !safeID.MatchString(*pointers.Previous) {
		return installedPointers{}, false
	}
	return pointers, true
}

func installedPython(root string) (string, error) {
	pointers, ok := readInstalledPointers(root)
	if !ok {
		return "", fmt.Errorf("installed Runtime pointer is invalid")
	}
	payload := filepath.Join(root, "slots", pointers.Current, "payload")
	python := filepath.Join(payload, "bin", "pack-python", "bin", "python3")
	if runtime.GOOS == "windows" {
		python = filepath.Join(payload, "bin", "pack-python", "python.exe")
	}
	info, err := os.Lstat(python)
	if err != nil ||
		!info.Mode().IsRegular() ||
		info.Mode()&os.ModeSymlink != 0 ||
		info.Size() < 1 {
		return "", fmt.Errorf("installed Runtime Python is unsafe")
	}
	return python, nil
}

func slicesContain(values []string, expected string) bool {
	if len(values) < 1 || len(values) > 3 {
		return false
	}
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		if !safeID.MatchString(value) {
			return false
		}
		if _, duplicated := seen[value]; duplicated {
			return false
		}
		seen[value] = struct{}{}
	}
	_, present := seen[expected]
	return present
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
	if err := validateBootstrapSigningRoles(keys, publicationKeys); err != nil {
		return config{}, "", nil, nil, err
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

func validateBootstrapSigningRoles(
	releaseKeys map[string]ed25519.PublicKey,
	publicationKeys map[string]ed25519.PublicKey,
) error {
	for keyID, releaseKey := range releaseKeys {
		if _, duplicated := publicationKeys[keyID]; duplicated {
			return fmt.Errorf("Bootstrap signing roles are not separated")
		}
		for _, publicationKey := range publicationKeys {
			if bytes.Equal(releaseKey, publicationKey) {
				return fmt.Errorf("Bootstrap signing roles are not separated")
			}
		}
	}
	return nil
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
		if info.Size() < 1 || info.Size() > 2*1024*1024 {
			return "", fmt.Errorf("Bootstrap sandbox helper destination is unsafe")
		}
		previous, readErr := os.ReadFile(destination)
		if readErr != nil || int64(len(previous)) != info.Size() {
			return "", fmt.Errorf("Bootstrap sandbox helper destination is unreadable")
		}
		if err := retainSandboxHelper(root, previous, sha256Hex(previous)); err != nil {
			return "", err
		}
	} else if !errors.Is(inspectErr, os.ErrNotExist) {
		return "", fmt.Errorf("Bootstrap sandbox helper destination is unavailable")
	}
	if err := retainSandboxHelper(root, payload, expectedDigest); err != nil {
		return "", err
	}
	if err := atomicWrite(destination, payload, 0o700); err != nil || !fileMatches(destination, metadata.Size(), expectedDigest) {
		return "", fmt.Errorf("Bootstrap sandbox helper could not be staged")
	}
	return destination, nil
}

func retainSandboxHelper(root string, payload []byte, expectedDigest string) error {
	if len(payload) < 1 || len(payload) > 2*1024*1024 || !sha256Pattern.MatchString(expectedDigest) || sha256Hex(payload) != expectedDigest {
		return fmt.Errorf("sandbox helper retention identity is invalid")
	}
	helpers := filepath.Join(root, "bootstrap", "helpers")
	directory := filepath.Join(helpers, expectedDigest)
	if err := os.Mkdir(helpers, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return fmt.Errorf("sandbox helper retention directory is unavailable")
	}
	for _, candidate := range []string{root, filepath.Join(root, "bootstrap"), helpers} {
		info, inspectErr := os.Lstat(candidate)
		resolved, resolveErr := filepath.EvalSymlinks(candidate)
		absolute, absoluteErr := filepath.Abs(candidate)
		resolvedAbsolute, resolvedAbsoluteErr := filepath.Abs(resolved)
		if inspectErr != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || resolveErr != nil || absoluteErr != nil || resolvedAbsoluteErr != nil || !strings.EqualFold(filepath.Clean(absolute), filepath.Clean(resolvedAbsolute)) {
			return fmt.Errorf("sandbox helper retention directory is unsafe")
		}
	}
	if err := os.Mkdir(directory, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return fmt.Errorf("sandbox helper retention directory is unavailable")
	}
	info, inspectErr := os.Lstat(directory)
	resolved, resolveErr := filepath.EvalSymlinks(directory)
	absolute, absoluteErr := filepath.Abs(directory)
	resolvedAbsolute, resolvedAbsoluteErr := filepath.Abs(resolved)
	if inspectErr != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || resolveErr != nil || absoluteErr != nil || resolvedAbsoluteErr != nil || !strings.EqualFold(filepath.Clean(absolute), filepath.Clean(resolvedAbsolute)) {
		return fmt.Errorf("sandbox helper retention directory is unsafe")
	}
	destination := filepath.Join(directory, "ecorex-sandbox-host.exe")
	if info, err := os.Lstat(destination); err == nil {
		if !info.Mode().IsRegular() || info.Mode()&os.ModeSymlink != 0 || !fileMatches(destination, int64(len(payload)), expectedDigest) {
			return fmt.Errorf("immutable sandbox helper retention conflicts with its digest")
		}
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("sandbox helper retention destination is unavailable")
	}
	if err := atomicWrite(destination, payload, 0o700); err != nil || !fileMatches(destination, int64(len(payload)), expectedDigest) {
		return fmt.Errorf("sandbox helper could not enter immutable retention")
	}
	return nil
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

func ensureRuntimeDataDirectories(root string) error {
	rootAbsolute, err := filepath.Abs(filepath.Clean(root))
	if err != nil {
		return fmt.Errorf("install root is invalid")
	}
	resolvedRoot, err := filepath.EvalSymlinks(rootAbsolute)
	if err != nil || !samePath(rootAbsolute, resolvedRoot) {
		return fmt.Errorf("install root contains a link or reparse point")
	}
	directories := []string{
		filepath.Join(rootAbsolute, "state"),
		filepath.Join(rootAbsolute, "state", "extension-cas"),
		filepath.Join(rootAbsolute, "workspace"),
	}
	for _, directory := range directories {
		if err := os.MkdirAll(directory, 0o700); err != nil {
			return fmt.Errorf("Runtime data directory is unavailable")
		}
		metadata, inspectErr := os.Lstat(directory)
		resolved, resolveErr := filepath.EvalSymlinks(directory)
		if inspectErr != nil || !metadata.IsDir() || metadata.Mode()&os.ModeSymlink != 0 || resolveErr != nil || !samePath(directory, resolved) {
			return fmt.Errorf("Runtime data directory is unsafe")
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

func loadTrustedLocalConfig(root string) (legacySelection, bool, error) {
	configPath := filepath.Join(root, "bootstrap", "bootstrap-local.json")
	metadata, err := os.Lstat(configPath)
	if errors.Is(err, os.ErrNotExist) {
		return legacySelection{}, false, nil
	}
	if err != nil || !metadata.Mode().IsRegular() || metadata.Mode()&os.ModeSymlink != 0 || metadata.Size() < 1 || metadata.Size() > 16*1024 {
		return legacySelection{}, false, fmt.Errorf("local Bootstrap configuration is unsafe")
	}
	if err := validateTrustedLocalConfigFile(configPath); err != nil {
		return legacySelection{}, false, fmt.Errorf("local Bootstrap configuration is not administrator-owned")
	}
	file, err := os.Open(configPath)
	if err != nil {
		return legacySelection{}, false, fmt.Errorf("local Bootstrap configuration is unreadable")
	}
	payload, readErr := io.ReadAll(io.LimitReader(file, 16*1024+1))
	closeErr := file.Close()
	after, statErr := os.Lstat(configPath)
	if readErr != nil || closeErr != nil || statErr != nil || !os.SameFile(metadata, after) || len(payload) < 1 || len(payload) > 16*1024 || validateTrustedLocalConfigFile(configPath) != nil {
		return legacySelection{}, false, fmt.Errorf("local Bootstrap configuration changed while reading")
	}
	var value localConfig
	if decodeExact(payload, &value) != nil || value.SchemaVersion != 1 {
		return legacySelection{}, false, fmt.Errorf("local Bootstrap configuration is invalid")
	}
	if value.LegacyV030Source != "" && value.LegacySource != "" {
		return legacySelection{}, false, fmt.Errorf("local Bootstrap configuration selects multiple legacy sources")
	}
	sourceValue := value.LegacySource
	version := value.LegacySourceVersion
	if value.LegacyV030Source != "" {
		sourceValue = value.LegacyV030Source
		version = "0.3.0"
	}
	if sourceValue == "" {
		return legacySelection{}, false, fmt.Errorf("local Bootstrap configuration is invalid")
	}
	if version == "" {
		version = "0.3.0"
	}
	if version != "0.2.9.2" && version != "0.3.0" {
		return legacySelection{}, false, fmt.Errorf("local Bootstrap configuration selects an unsupported legacy version")
	}
	source, err := canonicalLegacySource(sourceValue, root)
	if err != nil {
		return legacySelection{}, false, err
	}
	evidence := ""
	if value.LegacyReleaseEvidence != "" {
		evidence, err = canonicalLegacyEvidence(value.LegacyReleaseEvidence)
		if err != nil {
			return legacySelection{}, false, err
		}
	}
	if version == "0.2.9.2" && evidence == "" {
		return legacySelection{}, false, fmt.Errorf("v0.2.9.2 migration requires released Runtime evidence")
	}
	return legacySelection{Source: source, SourceVersion: version, ReleaseEvidence: evidence}, true, nil
}

func selectLegacyMigration(root string) (legacySelection, error) {
	// A persisted plan or completion is the migration authority. Re-discovery
	// must not rewrite it or require a legacy install that the user removed.
	for _, marker := range []string{
		filepath.Join(root, "migration", "v030-plan.json"),
		filepath.Join(root, "migration", "v030-completed.json"),
	} {
		if _, markerErr := os.Lstat(marker); markerErr == nil {
			return legacySelection{}, nil
		} else if !errors.Is(markerErr, os.ErrNotExist) {
			return legacySelection{}, fmt.Errorf("migration authority is unreadable")
		}
	}
	configured, present, err := loadTrustedLocalConfig(root)
	if err != nil {
		return legacySelection{}, err
	}
	if present {
		return configured, nil
	}
	home, homeErr := os.UserHomeDir()
	if homeErr != nil || !filepath.IsAbs(home) {
		return legacySelection{}, nil
	}
	legacyInstall := ""
	if runtime.GOOS == "windows" {
		local := os.Getenv("LOCALAPPDATA")
		if local == "" || !filepath.IsAbs(local) {
			return legacySelection{}, nil
		}
		legacyInstall = filepath.Join(local, "EcoreX WebUI")
	} else if runtime.GOOS == "darwin" {
		legacyInstall = filepath.Join(home, "Library", "Application Support", "EcoreX WebUI")
	} else {
		return legacySelection{}, nil
	}
	return discoverReleasedV0292(root, home, legacyInstall)
}

func discoverReleasedV0292(root, home, legacyInstall string) (legacySelection, error) {
	sourceCandidate := filepath.Join(home, "EcoreX")
	source, err := canonicalLegacySource(sourceCandidate, root)
	if err != nil {
		return legacySelection{}, nil
	}
	// This is the canonical conversation store path used by the released
	// v0.2.9.2 workspace. Only file metadata is inspected here.
	conversationDB := filepath.Join(source, "memory", "long-term", "index.db")
	if !safeNonemptyRegularFile(conversationDB, 4*1024*1024*1024) {
		return legacySelection{}, nil
	}
	installMetadata, err := os.Lstat(legacyInstall)
	if err != nil || !installMetadata.IsDir() || installMetadata.Mode()&os.ModeSymlink != 0 {
		return legacySelection{}, nil
	}
	installResolved, err := filepath.EvalSymlinks(legacyInstall)
	if err != nil || !samePath(legacyInstall, installResolved) {
		return legacySelection{}, nil
	}
	pointer := filepath.Join(legacyInstall, "state", "current-runtime.txt")
	runtimePath, err := readLegacyRuntimePointer(pointer, legacyInstall)
	if err != nil {
		return legacySelection{}, nil
	}
	evidence, err := canonicalLegacyEvidence(filepath.Join(runtimePath, "runtime-manifest.json"))
	if err != nil || !releasedV0292Manifest(evidence) {
		return legacySelection{}, nil
	}
	return legacySelection{
		Source:          source,
		SourceVersion:   "0.2.9.2",
		ReleaseEvidence: evidence,
	}, nil
}

func readLegacyRuntimePointer(pointer, legacyInstall string) (string, error) {
	payload, err := readStableRegularFile(pointer, 4096)
	if err != nil {
		return "", fmt.Errorf("legacy Runtime pointer is unreadable")
	}
	value := strings.TrimSpace(strings.TrimPrefix(string(payload), "\ufeff"))
	if strings.ContainsAny(value, "\x00\r\n") || !filepath.IsAbs(value) {
		return "", fmt.Errorf("legacy Runtime pointer is invalid")
	}
	absolute := filepath.Clean(value)
	leaf := filepath.Base(absolute)
	if !samePath(filepath.Dir(absolute), legacyInstall) || !strings.HasPrefix(leaf, "runtime-0.2.9.2-") || !safeID.MatchString(leaf) {
		return "", fmt.Errorf("legacy Runtime pointer is outside the released install")
	}
	metadata, err := os.Lstat(absolute)
	resolved, resolveErr := filepath.EvalSymlinks(absolute)
	if err != nil || !metadata.IsDir() || metadata.Mode()&os.ModeSymlink != 0 || resolveErr != nil || !samePath(absolute, resolved) {
		return "", fmt.Errorf("legacy Runtime directory is unsafe")
	}
	return absolute, nil
}

func safeNonemptyRegularFile(fileName string, maximum int64) bool {
	metadata, err := os.Lstat(fileName)
	if err != nil || !metadata.Mode().IsRegular() || metadata.Mode()&os.ModeSymlink != 0 || metadata.Size() < 1 || metadata.Size() > maximum {
		return false
	}
	resolved, err := filepath.EvalSymlinks(fileName)
	return err == nil && samePath(fileName, resolved)
}

func readStableRegularFile(fileName string, maximum int64) ([]byte, error) {
	metadata, err := os.Lstat(fileName)
	if err != nil || !metadata.Mode().IsRegular() || metadata.Mode()&os.ModeSymlink != 0 || metadata.Size() < 1 || metadata.Size() > maximum {
		return nil, fmt.Errorf("file is not a bounded regular file")
	}
	file, err := os.Open(fileName)
	if err != nil {
		return nil, err
	}
	payload, readErr := io.ReadAll(io.LimitReader(file, maximum+1))
	closeErr := file.Close()
	after, statErr := os.Lstat(fileName)
	if readErr != nil || closeErr != nil || statErr != nil || !os.SameFile(metadata, after) || int64(len(payload)) != metadata.Size() || int64(len(payload)) > maximum {
		return nil, fmt.Errorf("file changed while reading")
	}
	return payload, nil
}

func canonicalLegacyEvidence(value string) (string, error) {
	if !filepath.IsAbs(value) || strings.ContainsAny(value, "\x00\r\n") {
		return "", fmt.Errorf("legacy release evidence must be an absolute local path")
	}
	absolute := filepath.Clean(value)
	if !safeNonemptyRegularFile(absolute, 1024*1024) {
		return "", fmt.Errorf("legacy release evidence is unsafe")
	}
	return absolute, nil
}

func releasedV0292Manifest(fileName string) bool {
	payload, err := readStableRegularFile(fileName, 1024*1024)
	if err != nil {
		return false
	}
	var value legacyRuntimeManifest
	if json.Unmarshal(payload, &value) != nil {
		return false
	}
	return value.SchemaVersion == "v0.2.5-runtime-manifest-v1" &&
		value.Product == "EcoreX" && value.Version == "0.2.9.2" &&
		value.ReleaseGate.InstallReady
}

func canonicalLegacySource(value, root string) (string, error) {
	if !filepath.IsAbs(value) || strings.ContainsAny(value, "\x00\r\n") {
		return "", fmt.Errorf("legacy source must be an absolute local path")
	}
	absolute, err := filepath.Abs(filepath.Clean(value))
	if err != nil {
		return "", fmt.Errorf("legacy source is invalid")
	}
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		return "", fmt.Errorf("legacy source is unavailable")
	}
	resolved, err = filepath.Abs(resolved)
	if err != nil || !samePath(absolute, resolved) {
		return "", fmt.Errorf("legacy source contains a link or reparse point")
	}
	current := absolute
	for {
		metadata, inspectErr := os.Lstat(current)
		if inspectErr != nil || !metadata.IsDir() || metadata.Mode()&os.ModeSymlink != 0 {
			return "", fmt.Errorf("legacy source path is unsafe")
		}
		if current == filepath.Dir(current) {
			break
		}
		current = filepath.Dir(current)
	}
	rootAbsolute, err := filepath.Abs(filepath.Clean(root))
	if err != nil || pathsOverlap(absolute, rootAbsolute) {
		return "", fmt.Errorf("legacy source overlaps the v1 install root")
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
		Timeout:       5 * time.Minute,
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
	if descriptor.FileName != "release-manifest.json" || !sha256Pattern.MatchString(descriptor.SHA256) || len(descriptor.Sources) < 1 || len(descriptor.Sources) > 3 {
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
		if index < len(discovery.Manifest.Sources) {
			discovered := discovery.Manifest.Sources[index]
			expectedManifestURL := strings.TrimRight(item.BaseURL, "/") + "/release-manifest.json"
			if discovered.SourceID != item.SourceID || discovered.Kind != item.Kind || discovered.Priority != item.Priority || discovered.URL != expectedManifestURL {
				return fmt.Errorf("public discovery source does not match the signed release")
			}
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
	if len(match) != 4 {
		return 0, fmt.Errorf("stable release version must be a final product SemVer")
	}
	major, majorErr := strconv.ParseInt(match[1], 10, 64)
	minor, minorErr := strconv.ParseInt(match[2], 10, 64)
	patch, patchErr := strconv.ParseInt(match[3], 10, 64)
	if majorErr != nil || minorErr != nil || patchErr != nil {
		return 0, fmt.Errorf("stable release version is invalid")
	}
	sequence := major*100_000_000 + minor*10_000 + patch + 1
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

func requiredBootstrapArtifact(
	value *manifest,
	discovery *indexRelease,
	platform string,
	architecture string,
) (artifact, error) {
	if value == nil || discovery == nil {
		return artifact{}, fmt.Errorf("release Bootstrap descriptor is unavailable")
	}
	artifactID := "bootstrap-" + platform + "-" + architecture
	var selected *artifact
	for index := range value.Artifacts {
		item := &value.Artifacts[index]
		if item.ArtifactID != artifactID {
			continue
		}
		if selected != nil ||
			item.Platform != platform ||
			item.Architecture != architecture ||
			item.SizeBytes > maxBootstrapBytes {
			return artifact{}, fmt.Errorf("release Bootstrap descriptor is invalid")
		}
		selected = item
	}
	if selected == nil {
		return artifact{}, fmt.Errorf("release is missing the required Bootstrap")
	}
	var published *indexArtifact
	for index := range discovery.BootstrapArtifacts {
		item := &discovery.BootstrapArtifacts[index]
		if item.ArtifactID != artifactID {
			continue
		}
		if published != nil {
			return artifact{}, fmt.Errorf("public discovery repeats the host Bootstrap")
		}
		published = item
	}
	if published == nil ||
		published.Platform != selected.Platform ||
		published.Architecture != selected.Architecture ||
		published.FileName != selected.FileName ||
		published.SizeBytes != selected.SizeBytes ||
		published.SHA256 != selected.SHA256 ||
		published.Signature != selected.Signature ||
		len(published.Sources) < 1 ||
		len(published.Sources) > len(value.Sources) {
		return artifact{}, fmt.Errorf("public discovery does not bind the host Bootstrap")
	}
	signedSources := make(map[string]source, len(value.Sources))
	for _, origin := range value.Sources {
		signedSources[origin.SourceID] = origin
	}
	for index, discovered := range published.Sources {
		origin, signed := signedSources[discovered.SourceID]
		if !signed ||
			discovered.Kind != origin.Kind ||
			discovered.Priority != index ||
			validateHTTPS(discovered.URL) != nil {
			return artifact{}, fmt.Errorf("public discovery Bootstrap source is invalid")
		}
	}
	return *selected, nil
}

func downloadArtifact(
	ctx context.Context,
	client *http.Client,
	release *manifest,
	item artifact,
	destination string,
	keys map[string]ed25519.PublicKey,
	progresses ...*bootstrapProgress,
) error {
	var progress *bootstrapProgress
	if len(progresses) > 0 {
		progress = progresses[0]
	}
	if fileMatches(destination, item.SizeBytes, item.SHA256) {
		if err := verifyArtifactSignature(release, item, keys); err != nil {
			return err
		}
		progress.ArtifactCached(item)
		return nil
	}
	_ = os.Remove(destination)
	partial := destination + ".partial"
	if metadata, err := os.Lstat(partial); err == nil {
		if !metadata.Mode().IsRegular() || metadata.Mode()&os.ModeSymlink != 0 || metadata.Size() > item.SizeBytes {
			_ = os.Remove(partial)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("partial artifact is unavailable")
	}
	var last error
	for sourceIndex, origin := range release.Sources {
		location := strings.TrimRight(origin.BaseURL, "/") + "/" + url.PathEscape(item.FileName)
		current := int64(0)
		if metadata, statErr := os.Stat(partial); statErr == nil {
			current = metadata.Size()
		}
		progress.BeginSource(item, origin, current)
		if err := downloadFromSource(
			ctx,
			client,
			location,
			partial,
			item.SizeBytes,
			progress.UpdateDownload,
		); err != nil {
			last = err
			progress.SourceFailed(origin, sourceIndex+1 < len(release.Sources))
			continue
		}
		progress.VerifyingArtifact(item)
		if !fileMatches(partial, item.SizeBytes, item.SHA256) || verifyArtifactSignature(release, item, keys) != nil {
			last = fmt.Errorf("artifact verification failed")
			_ = os.Remove(partial)
			progress.ArtifactRejected(sourceIndex+1 < len(release.Sources))
			continue
		}
		if err := os.Rename(partial, destination); err != nil {
			return fmt.Errorf("verified artifact could not be committed")
		}
		progress.ArtifactComplete(item)
		return nil
	}
	return fmt.Errorf("all signed artifact sources failed: %w", last)
}

func downloadFromSource(
	ctx context.Context,
	client *http.Client,
	location string,
	destination string,
	expected int64,
	observers ...func(downloadProgress),
) error {
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
	notifyDownloadObservers(observers, resume, expected)
	for resume < expected {
		end := min(resume+artifactChunkBytes-1, expected-1)
		chunkSize := end - resume + 1
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, location, nil)
		if err != nil {
			return err
		}
		request.Header.Set("Accept-Encoding", "identity")
		request.Header.Set(
			"Range",
			fmt.Sprintf("bytes=%d-%d", resume, end),
		)
		response, err := client.Do(request)
		if err != nil {
			return err
		}
		expectedRange := fmt.Sprintf("bytes %d-%d/%d", resume, end, expected)
		if response.StatusCode != http.StatusPartialContent ||
			response.Header.Get("Content-Range") != expectedRange ||
			response.Header.Get("Content-Encoding") != "" && !strings.EqualFold(response.Header.Get("Content-Encoding"), "identity") ||
			response.ContentLength >= 0 && response.ContentLength != chunkSize {
			response.Body.Close()
			return fmt.Errorf("release source did not honor the bounded range")
		}
		flags := os.O_CREATE | os.O_WRONLY
		if resume > 0 {
			flags |= os.O_APPEND
		} else {
			flags |= os.O_EXCL
		}
		file, err := os.OpenFile(destination, flags, 0o600)
		if err != nil {
			response.Body.Close()
			return err
		}
		writer := &downloadProgressWriter{
			writer:     file,
			downloaded: resume,
			total:      expected,
			observers:  observers,
		}
		written, copyErr := io.CopyN(writer, response.Body, chunkSize)
		extra := make([]byte, 1)
		extraCount, extraErr := response.Body.Read(extra)
		bodyCloseErr := response.Body.Close()
		syncErr := file.Sync()
		closeErr := file.Close()
		if copyErr != nil || written != chunkSize || extraCount != 0 || extraErr != io.EOF || bodyCloseErr != nil || syncErr != nil || closeErr != nil {
			return fmt.Errorf("release source ended outside the signed artifact bound")
		}
		resume += chunkSize
	}
	return nil
}

type downloadProgressWriter struct {
	writer     io.Writer
	downloaded int64
	total      int64
	observers  []func(downloadProgress)
}

func (writer *downloadProgressWriter) Write(payload []byte) (int, error) {
	written, err := writer.writer.Write(payload)
	if written > 0 {
		writer.downloaded += int64(written)
		notifyDownloadObservers(
			writer.observers,
			writer.downloaded,
			writer.total,
		)
	}
	return written, err
}

func notifyDownloadObservers(
	observers []func(downloadProgress),
	downloaded int64,
	total int64,
) {
	value := downloadProgress{Downloaded: downloaded, Total: total}
	for _, observer := range observers {
		if observer != nil {
			observer(value)
		}
	}
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

func supervise(
	python string,
	root string,
	trusted []string,
	legacy legacySelection,
	ownerNonce string,
) error {
	arguments := superviseArguments(root, trusted, legacy)
	command := exec.Command(python, arguments...)
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	command.Stdin = os.Stdin
	command.Env = append(
		minimalEnvironment(),
		"ECOREX_RUNTIME_OWNER_NONCE="+ownerNonce,
	)
	if err := command.Run(); err != nil {
		return fmt.Errorf("installed Runtime did not pass Bootstrap health")
	}
	return nil
}

func superviseArguments(root string, trusted []string, legacy legacySelection) []string {
	arguments := []string{"-I", "-B", "-m", "ecorex.bootstrap", "--install-root", root}
	if legacy.Source != "" {
		arguments = append(arguments, "--legacy-source", legacy.Source, "--legacy-source-version", legacy.SourceVersion)
		if legacy.ReleaseEvidence != "" {
			arguments = append(arguments, "--legacy-release-evidence", legacy.ReleaseEvidence)
		}
	}
	for _, definition := range trusted {
		arguments = append(arguments, "--trusted-public-key", definition)
	}
	return arguments
}

func minimalEnvironment() []string {
	allowed := map[string]bool{
		"APPDATA":                true,
		"HOME":                   true,
		"LANG":                   true,
		"LC_ALL":                 true,
		"LOCALAPPDATA":           true,
		"PROCESSOR_ARCHITECTURE": true,
		"PROCESSOR_ARCHITEW6432": true,
		"SYSTEMDRIVE":            true,
		"SYSTEMROOT":             true,
		"TEMP":                   true,
		"TMP":                    true,
		"USERPROFILE":            true,
		"WINDIR":                 true,
	}
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

func fail(errorValue error) {
	fmt.Fprintln(os.Stderr)
	fmt.Fprintln(os.Stderr, "[未完成] "+userFacingFailure(errorValue))
	os.Exit(1)
}
