package main

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func canonicalTestTempDir(t *testing.T) string {
	t.Helper()
	raw := t.TempDir()
	resolved, err := filepath.EvalSymlinks(raw)
	if err != nil {
		t.Fatal(err)
	}
	absolute, err := filepath.Abs(resolved)
	if err != nil {
		t.Fatal(err)
	}
	metadata, err := os.Lstat(absolute)
	if err != nil || !metadata.IsDir() || metadata.Mode()&os.ModeSymlink != 0 {
		t.Fatalf("canonical test directory is unsafe: %v", err)
	}
	return filepath.Clean(absolute)
}

func signedManifest(t *testing.T) (*manifest, *indexRelease, map[string]ed25519.PublicKey) {
	t.Helper()
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	placeholder := signature{Algorithm: "ed25519", KeyID: "release-key", Value: base64.StdEncoding.EncodeToString(make([]byte, ed25519.SignatureSize))}
	sources := []source{
		{SourceID: "mirror", Kind: "github-cn-mirror", Priority: 0, BaseURL: "https://mirror.example/v1"},
		{SourceID: "github", Kind: "github-release", Priority: 1, BaseURL: "https://github.example/v1"},
		{SourceID: "cdn", Kind: "ecorex-cdn", Priority: 2, BaseURL: "https://cdn.example/v1"},
	}
	value := &manifest{
		SchemaVersion: 1,
		ReleaseID:     "release-1.0.0-stable",
		Version:       "1.0.0",
		BuildDigest:   hex.EncodeToString(make([]byte, sha256.Size)),
		Channel:       "stable",
		CreatedAt:     "2026-07-11T00:00:00Z",
		Sources:       sources,
		Artifacts: []artifact{{
			ArtifactID: "core-windows-x64", Platform: "windows", Architecture: "x64",
			FileName: "core.zip", SizeBytes: 1, SHA256: hex.EncodeToString(make([]byte, sha256.Size)), Signature: placeholder,
		}},
		Signature: placeholder,
	}
	payload, err := canonicalManifestPayload(value)
	if err != nil {
		t.Fatal(err)
	}
	value.Signature.Value = base64.StdEncoding.EncodeToString(ed25519.Sign(private, payload))
	indexSources := make([]indexSource, len(sources))
	for position, item := range sources {
		indexSources[position] = indexSource{item.SourceID, item.Kind, item.Priority, item.BaseURL + "/release-manifest.json"}
	}
	discovery := &indexRelease{
		ReleaseID: value.ReleaseID, Version: value.Version, Channel: value.Channel,
		BuildDigest: value.BuildDigest,
		Manifest:    indexManifest{FileName: "release-manifest.json", SHA256: value.BuildDigest, Signature: value.Signature, Sources: indexSources},
	}
	return value, discovery, map[string]ed25519.PublicKey{"release-key": public}
}

func TestManifestSignatureAndSourceBinding(t *testing.T) {
	value, discovery, keys := signedManifest(t)
	if err := validateManifest(value, discovery, keys); err != nil {
		t.Fatal(err)
	}
	discovery.Manifest.Sources[0].URL = "https://cdn.example/replayed.json"
	if err := validateManifest(value, discovery, keys); err == nil {
		t.Fatal("unbound discovery source was accepted")
	}
}

func TestResumeDownloadRequiresExactContentRange(t *testing.T) {
	payload := []byte("0123456789abcdef")
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		start := int64(0)
		if raw := request.Header.Get("Range"); raw != "" {
			if _, err := fmt.Sscanf(raw, "bytes=%d-", &start); err != nil {
				t.Fatal(err)
			}
		}
		body := payload[start:]
		headers := make(http.Header)
		status := http.StatusOK
		if start > 0 {
			status = http.StatusPartialContent
			headers.Set("Content-Range", "bytes "+strconv.FormatInt(start, 10)+"-"+strconv.Itoa(len(payload)-1)+"/"+strconv.Itoa(len(payload)))
		}
		return &http.Response{
			StatusCode:    status,
			Header:        headers,
			Body:          io.NopCloser(bytes.NewReader(body)),
			ContentLength: int64(len(body)),
			Request:       request,
		}, nil
	})}
	directory := t.TempDir()
	destination := filepath.Join(directory, "artifact.partial")
	if err := os.WriteFile(destination, payload[:5], 0o600); err != nil {
		t.Fatal(err)
	}
	if err := downloadFromSource(context.Background(), client, "https://download.example/artifact", destination, int64(len(payload))); err != nil {
		t.Fatal(err)
	}
	observed, err := os.ReadFile(destination)
	if err != nil || !bytes.Equal(observed, payload) {
		t.Fatalf("resumed bytes mismatch: %v", err)
	}
}

func TestDiscoveryUsesBoundedHTTPSServerClock(t *testing.T) {
	payload := []byte(`{"status":"published"}`)
	serverTime := time.Now().UTC().Truncate(time.Second)
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		headers := make(http.Header)
		headers.Set("Date", serverTime.Format(http.TimeFormat))
		return &http.Response{
			StatusCode:    http.StatusOK,
			Header:        headers,
			Body:          io.NopCloser(bytes.NewReader(payload)),
			ContentLength: int64(len(payload)),
			Request:       request,
		}, nil
	})}
	observed, trusted, err := fetchDiscovery(
		context.Background(), client, "https://control.example/index.json", 1024,
	)
	if err != nil || !bytes.Equal(observed, payload) || !trusted.Equal(serverTime) {
		t.Fatalf("trusted discovery clock mismatch: %v", err)
	}
	client.Transport = roundTripFunc(func(request *http.Request) (*http.Response, error) {
		headers := make(http.Header)
		headers.Set("Date", serverTime.Add(-25*time.Hour).Format(http.TimeFormat))
		return &http.Response{
			StatusCode:    http.StatusOK,
			Header:        headers,
			Body:          io.NopCloser(bytes.NewReader(payload)),
			ContentLength: int64(len(payload)),
			Request:       request,
		}, nil
	})
	if _, _, err := fetchDiscovery(
		context.Background(), client, "https://control.example/index.json", 1024,
	); err == nil {
		t.Fatal("an implausibly stale HTTPS clock was accepted")
	}
}

func TestCoreExtractionRejectsTraversal(t *testing.T) {
	archivePath := filepath.Join(t.TempDir(), "core.zip")
	file, err := os.Create(archivePath)
	if err != nil {
		t.Fatal(err)
	}
	archive := zip.NewWriter(file)
	entry, err := archive.Create("../escape.txt")
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.WriteString(entry, "unsafe")
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := file.Close(); err != nil {
		t.Fatal(err)
	}
	if err := extractCore(archivePath, filepath.Join(t.TempDir(), "out")); err == nil {
		t.Fatal("path traversal archive was accepted")
	}
}

func TestSafeFileNameRejectsPlatformEscapes(t *testing.T) {
	for _, value := range []string{"../x", "a/b", `a\\b`, "C:drive", "line\nbreak"} {
		if safeFileName(value) {
			t.Fatalf("unsafe file name was accepted: %q", value)
		}
	}
}

func signedPointer(
	t *testing.T,
	private ed25519.PrivateKey,
	version string,
	releaseID string,
	manifestDigest string,
	buildDigest string,
) pointerAuthority {
	t.Helper()
	sequence, err := stableReleaseSequence(version)
	if err != nil {
		t.Fatal(err)
	}
	value := pointerAuthority{
		Sequence: sequence,
		Revision: releaseID,
		Target: authorityTarget{
			ManifestSHA256: manifestDigest,
			ReleaseID:      releaseID,
			Version:        version,
			BuildDigest:    buildDigest,
		},
		Signature: signature{
			Algorithm: "ed25519",
			KeyID:     "release-key",
		},
	}
	value.Signature.Value = base64.StdEncoding.EncodeToString(
		ed25519.Sign(private, pointerAuthorityPayload(value)),
	)
	return value
}

func signedMinimum(
	t *testing.T,
	private ed25519.PrivateKey,
	version string,
) minimumStable {
	t.Helper()
	sequence, err := stableReleaseSequence(version)
	if err != nil {
		t.Fatal(err)
	}
	value := minimumStable{
		Sequence: sequence,
		Version:  version,
		Signature: signature{
			Algorithm: "ed25519",
			KeyID:     "release-key",
		},
	}
	payload := []byte(fmt.Sprintf(
		"ecorex.bootstrap-minimum-stable.v1%c%d%c%s",
		0,
		value.Sequence,
		0,
		value.Version,
	))
	value.Signature.Value = base64.StdEncoding.EncodeToString(
		ed25519.Sign(private, payload),
	)
	return value
}

func signedFreshness(
	t *testing.T,
	private ed25519.PrivateKey,
	authority pointerAuthority,
	issuedAt time.Time,
	expiresAt time.Time,
) pointerFreshness {
	t.Helper()
	value := pointerFreshness{
		AuthoritySHA256: pointerAuthoritySHA256(authority),
		IssuedAt:        issuedAt.UTC().Truncate(time.Second).Format("2006-01-02T15:04:05Z"),
		ExpiresAt:       expiresAt.UTC().Truncate(time.Second).Format("2006-01-02T15:04:05Z"),
		Signature: signature{
			Algorithm: "ed25519",
			KeyID:     "publication-key",
		},
	}
	value.Signature.Value = base64.StdEncoding.EncodeToString(
		ed25519.Sign(private, pointerFreshnessPayload(value)),
	)
	return value
}

func TestPointerAuthorityIsSignedBoundAndMonotonic(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	keys := map[string]ed25519.PublicKey{"release-key": public}
	publicationPublic, publicationPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	publicationKeys := map[string]ed25519.PublicKey{
		"publication-key": publicationPublic,
	}
	trustedNow := time.Date(2026, 7, 11, 12, 0, 0, 0, time.UTC)
	manifestDigest := fmt.Sprintf("%064x", 1)
	firstBuild := fmt.Sprintf("%064x", 2)
	firstRelease := "release-stable-000000000000000000000001"
	first := signedPointer(
		t, private, "1.0.0", firstRelease, manifestDigest, firstBuild,
	)
	index := publicIndex{
		SchemaVersion: 1,
		DocumentType:  "ecorex.public-bootstrap-discovery",
		Trust:         "untrusted-discovery-hint",
		Status:        "published",
		Authority:     &first,
		Release: &indexRelease{
			ReleaseID:   firstRelease,
			Version:     "1.0.0",
			Channel:     "stable",
			BuildDigest: firstBuild,
			Manifest: indexManifest{
				FileName: "release-manifest.json",
				SHA256:   manifestDigest,
			},
		},
	}
	if err := validatePointerAuthority(
		&index,
		keys,
		signedMinimum(t, private, "1.0.0"),
	); err != nil {
		t.Fatal(err)
	}
	root := canonicalTestTempDir(t)
	if err := ensureBootstrapStateDirectory(root); err != nil {
		t.Fatal(err)
	}
	firstFreshness := signedFreshness(
		t, publicationPrivate, first,
		trustedNow.Add(-time.Hour), trustedNow.Add(12*time.Hour),
	)
	if err := acceptPointerAuthority(root, first, firstFreshness, keys, publicationKeys, trustedNow); err != nil {
		t.Fatal(err)
	}
	if err := acceptPointerAuthority(root, first, firstFreshness, keys, publicationKeys, trustedNow); err != nil {
		t.Fatalf("exact accepted target must be idempotent: %v", err)
	}
	second := signedPointer(
		t,
		private,
		"1.0.1",
		"release-stable-000000000000000000000002",
		fmt.Sprintf("%064x", 3),
		fmt.Sprintf("%064x", 4),
	)
	secondFreshness := signedFreshness(
		t, publicationPrivate, second,
		trustedNow.Add(-30*time.Minute), trustedNow.Add(16*time.Hour),
	)
	if err := acceptPointerAuthority(root, second, secondFreshness, keys, publicationKeys, trustedNow); err != nil {
		t.Fatal(err)
	}
	if err := acceptPointerAuthority(root, first, firstFreshness, keys, publicationKeys, trustedNow); err == nil {
		t.Fatal("a lower signed sequence was accepted after a newer target")
	}
	rebuilt := signedPointer(
		t,
		private,
		"1.0.1",
		"release-stable-000000000000000000000003",
		fmt.Sprintf("%064x", 5),
		fmt.Sprintf("%064x", 6),
	)
	rebuiltFreshness := signedFreshness(
		t, publicationPrivate, rebuilt,
		trustedNow, trustedNow.Add(20*time.Hour),
	)
	if err := acceptPointerAuthority(root, rebuilt, rebuiltFreshness, keys, publicationKeys, trustedNow); err == nil {
		t.Fatal("the same sequence was replayed with another signed target")
	}
	statePayload, err := os.ReadFile(filepath.Join(root, "bootstrap", "pointer-authority.json"))
	if err != nil {
		t.Fatal(err)
	}
	var state pointerState
	if err := json.Unmarshal(statePayload, &state); err != nil || state.Authority != second || state.Freshness != secondFreshness {
		t.Fatalf("persisted authority mismatch: %v", err)
	}
}

func TestPointerFreshnessIsShortLivedRoleSeparatedAndMonotonic(t *testing.T) {
	releasePublic, releasePrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	publicationPublic, publicationPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	releaseKeys := map[string]ed25519.PublicKey{"release-key": releasePublic}
	publicationKeys := map[string]ed25519.PublicKey{
		"publication-key": publicationPublic,
	}
	authority := signedPointer(
		t,
		releasePrivate,
		"1.0.0",
		"release-stable-000000000000000000000010",
		fmt.Sprintf("%064x", 10),
		fmt.Sprintf("%064x", 11),
	)
	now := time.Date(2026, 7, 11, 12, 0, 0, 0, time.UTC)
	initial := signedFreshness(
		t, publicationPrivate, authority, now.Add(-time.Hour), now.Add(time.Hour),
	)
	if err := validatePointerFreshness(authority, initial, publicationKeys, now); err != nil {
		t.Fatal(err)
	}
	root := canonicalTestTempDir(t)
	if err := ensureBootstrapStateDirectory(root); err != nil {
		t.Fatal(err)
	}
	if err := acceptPointerAuthority(root, authority, initial, releaseKeys, publicationKeys, now); err != nil {
		t.Fatal(err)
	}
	renewed := signedFreshness(
		t,
		publicationPrivate,
		authority,
		now.Add(-30*time.Minute),
		now.Add(4*time.Hour),
	)
	if err := acceptPointerAuthority(root, authority, renewed, releaseKeys, publicationKeys, now); err != nil {
		t.Fatalf("a valid same-target freshness renewal was rejected: %v", err)
	}
	if err := acceptPointerAuthority(root, authority, initial, releaseKeys, publicationKeys, now); err == nil {
		t.Fatal("a stale same-target freshness envelope was accepted")
	}

	expired := signedFreshness(
		t, publicationPrivate, authority, now.Add(-time.Hour), now,
	)
	if err := validatePointerFreshness(authority, expired, publicationKeys, now); err == nil {
		t.Fatal("freshness expiring exactly at trusted now was accepted")
	}
	future := signedFreshness(
		t,
		publicationPrivate,
		authority,
		now.Add(5*time.Minute+time.Second),
		now.Add(6*time.Minute),
	)
	if err := validatePointerFreshness(authority, future, publicationKeys, now); err == nil {
		t.Fatal("freshness issued outside the future-skew bound was accepted")
	}
	tooLong := signedFreshness(
		t,
		publicationPrivate,
		authority,
		now,
		now.Add(24*time.Hour+time.Second),
	)
	if err := validatePointerFreshness(authority, tooLong, publicationKeys, now); err == nil {
		t.Fatal("freshness exceeding the 24-hour product TTL was accepted")
	}
	roleConfused := initial
	roleConfused.Signature.KeyID = authority.Signature.KeyID
	roleConfused.Signature.Value = base64.StdEncoding.EncodeToString(
		ed25519.Sign(releasePrivate, pointerFreshnessPayload(roleConfused)),
	)
	if err := validatePointerFreshness(
		authority,
		roleConfused,
		map[string]ed25519.PublicKey{"release-key": releasePublic},
		now,
	); err == nil {
		t.Fatal("the release signing role was accepted for online freshness")
	}
}

func TestPointerAuthorityHashMatchesTheCrossLanguageSigningVector(t *testing.T) {
	authority := pointerAuthority{
		Sequence: 1,
		Revision: "release-stable-000000000000000000000001",
		Target: authorityTarget{
			ManifestSHA256: strings.Repeat("0", 64),
			ReleaseID:      "release-stable-000000000000000000000001",
			Version:        "1.0.0",
			BuildDigest:    strings.Repeat("1", 64),
		},
	}
	if observed := pointerAuthoritySHA256(authority); observed != "cc915ef3d060ce8924de34e232c4c2d934971d502de7f1aa5e4ecbab6622767f" {
		t.Fatalf("cross-language authority hash mismatch: %s", observed)
	}
}

func TestFreshInstallRejectsPointerBelowSignedBootstrapFloor(t *testing.T) {
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	keys := map[string]ed25519.PublicKey{"release-key": public}
	manifestDigest := fmt.Sprintf("%064x", 7)
	buildDigest := fmt.Sprintf("%064x", 8)
	releaseID := "release-stable-000000000000000000000007"
	authority := signedPointer(
		t, private, "1.0.0", releaseID, manifestDigest, buildDigest,
	)
	index := publicIndex{
		SchemaVersion: 1,
		DocumentType:  "ecorex.public-bootstrap-discovery",
		Trust:         "untrusted-discovery-hint",
		Status:        "published",
		Authority:     &authority,
		Release: &indexRelease{
			ReleaseID:   releaseID,
			Version:     "1.0.0",
			Channel:     "stable",
			BuildDigest: buildDigest,
			Manifest: indexManifest{
				FileName: "release-manifest.json",
				SHA256:   manifestDigest,
			},
		},
	}
	if err := validatePointerAuthority(
		&index,
		keys,
		signedMinimum(t, private, "1.0.1"),
	); err == nil {
		t.Fatal("fresh install accepted a pointer below its signed Bootstrap floor")
	}
}

func TestPointerAuthorityRejectsNonFinalOrPreV1Version(t *testing.T) {
	for _, version := range []string{"0.9.9", "1.0.0-rc.1", "1.0.0+rebuilt", "2.0.0"} {
		if _, err := stableReleaseSequence(version); err == nil {
			t.Fatalf("non-final v1 target was accepted: %s", version)
		}
	}
}

func TestFreshBootstrapStateDirectoryAndTrustedLocalMigrationSource(t *testing.T) {
	parent := canonicalTestTempDir(t)
	root := filepath.Join(parent, "v1")
	legacy := filepath.Join(parent, "v030")
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(legacy, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := ensureBootstrapStateDirectory(root); err != nil {
		t.Fatal(err)
	}
	configuration, err := json.Marshal(localConfig{
		SchemaVersion:    1,
		LegacyV030Source: legacy,
	})
	if err != nil {
		t.Fatal(err)
	}
	configuration = append(configuration, '\n')
	path := filepath.Join(root, "bootstrap", "bootstrap-local.json")
	if err := os.WriteFile(path, configuration, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := hardenTestLocalConfig(path); err != nil {
		t.Fatal(err)
	}
	if err := validateTrustedLocalConfigFile(path); err != nil {
		t.Fatalf("test local configuration ACL is not trusted: %v", err)
	}
	observed, err := loadTrustedLocalConfig(root)
	if err != nil {
		t.Fatal(err)
	}
	expected, _ := filepath.Abs(legacy)
	if !samePath(observed, expected) {
		t.Fatalf("legacy source mismatch: %q != %q", observed, expected)
	}
	if _, err := canonicalLegacySource(root, root); err == nil {
		t.Fatal("overlapping legacy/v1 roots were accepted")
	}
}

func TestBoundedBufferFailsAtTheConfiguredLimit(t *testing.T) {
	buffer := boundedBuffer{limit: 4}
	if _, err := buffer.Write([]byte("1234")); err != nil {
		t.Fatal(err)
	}
	if _, err := buffer.Write([]byte("5")); err == nil || !buffer.overflow {
		t.Fatal("overflowing subprocess output was accepted")
	}
}

func TestRequiredArtifactsIncludesEveryProductCapabilityPack(t *testing.T) {
	target := "windows-x64"
	value := &manifest{Artifacts: []artifact{{
		ArtifactID: "core-" + target, Platform: "windows", Architecture: "x64",
	}}}
	packIDs := []string{"browser", "channels", "image", "ocr", "office", "sandbox"}
	for _, packID := range packIDs {
		base := "capability-pack-" + packID + "-" + target
		value.Artifacts = append(value.Artifacts,
			artifact{ArtifactID: base, Platform: "windows", Architecture: "x64"},
			artifact{ArtifactID: base + "-manifest", Platform: "windows", Architecture: "x64"},
		)
	}
	selected, err := requiredArtifacts(value, "windows", "x64")
	if err != nil || len(selected) != 1+2*len(packIDs) {
		t.Fatalf("complete product pack set was rejected: %v", err)
	}
	value.Artifacts = value.Artifacts[:len(value.Artifacts)-1]
	if _, err := requiredArtifacts(value, "windows", "x64"); err == nil {
		t.Fatal("release without a required capability pack manifest was accepted")
	}
	value.Artifacts = append(value.Artifacts,
		artifact{ArtifactID: "capability-pack-unknown-" + target, Platform: "windows", Architecture: "x64"},
	)
	if _, err := requiredArtifacts(value, "windows", "x64"); err == nil {
		t.Fatal("release with an unexpected host capability pack was accepted")
	}
}
