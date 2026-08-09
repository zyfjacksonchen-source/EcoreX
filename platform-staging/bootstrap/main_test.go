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
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
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

func TestSandboxHelperRetentionKeepsDigestVersionsImmutable(t *testing.T) {
	root := canonicalTestTempDir(t)
	if err := os.MkdirAll(filepath.Join(root, "bootstrap"), 0o700); err != nil {
		t.Fatal(err)
	}
	first := bytes.Repeat([]byte("first-helper"), 64)
	second := bytes.Repeat([]byte("second-helper"), 64)
	firstDigest := sha256Hex(first)
	secondDigest := sha256Hex(second)
	if err := retainSandboxHelper(root, first, firstDigest); err != nil {
		t.Fatal(err)
	}
	if err := retainSandboxHelper(root, second, secondDigest); err != nil {
		t.Fatal(err)
	}
	firstPath := filepath.Join(
		root,
		"bootstrap",
		"helpers",
		firstDigest,
		"ecorex-sandbox-host.exe",
	)
	secondPath := filepath.Join(
		root,
		"bootstrap",
		"helpers",
		secondDigest,
		"ecorex-sandbox-host.exe",
	)
	if !fileMatches(firstPath, int64(len(first)), firstDigest) {
		t.Fatal("the retained prior helper changed")
	}
	if !fileMatches(secondPath, int64(len(second)), secondDigest) {
		t.Fatal("the retained target helper did not verify")
	}
	if err := retainSandboxHelper(root, first, firstDigest); err != nil {
		t.Fatalf("idempotent helper retention failed: %v", err)
	}
	if err := os.WriteFile(firstPath, second, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := retainSandboxHelper(root, first, firstDigest); err == nil {
		t.Fatal("a digest-store conflict was accepted")
	}
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

func TestManifestAllowsSignedDiscoverySourcePrefix(t *testing.T) {
	value, discovery, keys := signedManifest(t)
	discovery.Manifest.Sources = discovery.Manifest.Sources[:1]
	if err := validateManifest(value, discovery, keys); err != nil {
		t.Fatalf("signed one-source Stable discovery was rejected: %v", err)
	}
	discovery.Manifest.Sources[0].URL = "https://mirror.example/replayed.json"
	if err := validateManifest(value, discovery, keys); err == nil {
		t.Fatal("unbound one-source discovery was accepted")
	}
}

func TestMinimalEnvironmentPreservesHostArchitectureWithoutSecrets(t *testing.T) {
	t.Setenv("PROCESSOR_ARCHITECTURE", "AMD64")
	t.Setenv("PROCESSOR_ARCHITEW6432", "AMD64")
	t.Setenv("ECOREX_TEST_SECRET", "must-not-cross")
	observed := map[string]string{}
	for _, item := range minimalEnvironment() {
		name, value, ok := strings.Cut(item, "=")
		if !ok {
			t.Fatalf("malformed environment entry: %q", item)
		}
		observed[strings.ToUpper(name)] = value
	}
	if observed["PROCESSOR_ARCHITECTURE"] != "AMD64" ||
		observed["PROCESSOR_ARCHITEW6432"] != "AMD64" {
		t.Fatal("minimal environment removed the Windows host architecture")
	}
	if _, leaked := observed["ECOREX_TEST_SECRET"]; leaked {
		t.Fatal("minimal environment leaked a non-allowlisted value")
	}
}

func TestRuntimeDataDirectoriesAreProvisionedBeforeFirstHealth(t *testing.T) {
	root := canonicalTestTempDir(t)
	if err := ensureRuntimeDataDirectories(root); err != nil {
		t.Fatalf("Runtime data directories were not provisioned: %v", err)
	}
	for _, name := range []string{"state", filepath.Join("state", "extension-cas"), "workspace"} {
		path := filepath.Join(root, name)
		metadata, err := os.Lstat(path)
		resolved, resolveErr := filepath.EvalSymlinks(path)
		if err != nil || !metadata.IsDir() || metadata.Mode()&os.ModeSymlink != 0 ||
			resolveErr != nil || !samePath(path, resolved) {
			t.Fatalf("Runtime data directory %q is unsafe: %v / %v", name, err, resolveErr)
		}
	}
}

func TestRuntimeDataDirectoryRejectsAFileCollision(t *testing.T) {
	root := canonicalTestTempDir(t)
	if err := os.WriteFile(filepath.Join(root, "state"), []byte("collision"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := ensureRuntimeDataDirectories(root); err == nil {
		t.Fatal("Runtime data directory accepted a file collision")
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
	observedProgress := []downloadProgress{}
	if err := downloadFromSource(
		context.Background(),
		client,
		"https://download.example/artifact",
		destination,
		int64(len(payload)),
		func(value downloadProgress) {
			observedProgress = append(observedProgress, value)
		},
	); err != nil {
		t.Fatal(err)
	}
	observed, err := os.ReadFile(destination)
	if err != nil || !bytes.Equal(observed, payload) {
		t.Fatalf("resumed bytes mismatch: %v", err)
	}
	if len(observedProgress) < 2 ||
		observedProgress[0].Downloaded != 5 ||
		observedProgress[0].Total != int64(len(payload)) ||
		observedProgress[len(observedProgress)-1].Downloaded != int64(len(payload)) {
		t.Fatalf("download progress did not preserve resume/final bytes: %#v", observedProgress)
	}
}

func TestBootstrapProgressShowsStageSpeedAndETA(t *testing.T) {
	var output bytes.Buffer
	progress := newBootstrapProgress(&output)
	now := time.Date(2026, 7, 19, 12, 0, 0, 0, time.UTC)
	progress.now = func() time.Time { return now }
	item := artifact{
		ArtifactID: "core-windows-x64",
		FileName:   "ecorex-core.zip",
		SizeBytes:  100 * 1024 * 1024,
	}
	progress.BeginArtifact(item, 1, 2)
	progress.BeginSource(
		item,
		source{Kind: "github-cn-mirror"},
		0,
	)
	now = now.Add(2 * time.Second)
	progress.UpdateDownload(downloadProgress{
		Downloaded: 50 * 1024 * 1024,
		Total:      item.SizeBytes,
	})
	progress.VerifyingArtifact(item)
	value := output.String()
	for _, expected := range []string{
		"[下载]",
		"(1/2)",
		"e-Mate 核心",
		"50%",
		"25.0 MiB/s",
		"剩余 2 秒",
		"国内镜像",
		"[校验]",
	} {
		if !strings.Contains(value, expected) {
			t.Fatalf("progress output is missing %q:\n%s", expected, value)
		}
	}
}

func TestBootstrapFailureMessagesRemainActionableAndSafe(t *testing.T) {
	cases := []struct {
		errorValue error
		expected   string
	}{
		{
			errorValue: errors.New("all signed artifact sources failed"),
			expected:   "检查网络",
		},
		{
			errorValue: errors.New("artifact verification failed"),
			expected:   "校验未通过",
		},
		{
			errorValue: errors.New("local release directory inventory is invalid"),
			expected:   "校验未通过",
		},
		{
			errorValue: errors.New("installed Runtime did not pass Bootstrap health"),
			expected:   "本地服务",
		},
	}
	for _, item := range cases {
		message := userFacingFailure(item.errorValue)
		if !strings.Contains(message, item.expected) ||
			strings.Contains(message, item.errorValue.Error()) {
			t.Fatalf("unsafe or unactionable failure message: %q", message)
		}
	}
}

func TestLocalReleaseEvidenceMatchesManifestAndSBOM(t *testing.T) {
	releaseDir := canonicalTestTempDir(t)
	if err := validateLocalReleaseEvidence(releaseDir, []byte(`{}`), &manifest{}); err != nil {
		t.Fatalf("signed-only outer package was rejected: %v", err)
	}
	sbomBytes := []byte(`{"bomFormat":"CycloneDX"}`)
	if err := os.WriteFile(filepath.Join(releaseDir, "sbom.cdx.json"), sbomBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	item := artifact{
		ArtifactID: "core-macos-arm64", Platform: "macos", Architecture: "arm64",
		FileName: "core.zip", SizeBytes: 1, SHA256: fmt.Sprintf("%064x", 2),
		Signature: signature{Algorithm: "ed25519", KeyID: "release-key", Value: "signature"},
	}
	release := &manifest{
		SchemaVersion: 1, ReleaseID: "release-stable-000000000000000000000001",
		Version: "1.0.0", Channel: "stable", CreatedAt: "2026-08-07T00:00:00Z",
		BuildDigest: fmt.Sprintf("%064x", 1), Artifacts: []artifact{item},
		Signature: signature{Algorithm: "ed25519", KeyID: "release-key", Value: "manifest-signature"},
	}
	manifestBytes := []byte(`{"signed":true}`)
	metadata := localReleaseMetadata{
		SchemaVersion: 1, ReleaseID: release.ReleaseID, Version: release.Version,
		Channel: release.Channel, CreatedAt: release.CreatedAt, BuildDigest: release.BuildDigest,
		Manifest: "release-manifest.json", ManifestSHA256: sha256Hex(manifestBytes),
		ManifestSignature: release.Signature, SBOM: "sbom.cdx.json", SBOMSHA256: sha256Hex(sbomBytes),
		Artifacts: []localReleaseMetadataArtifact{{
			ArtifactID: item.ArtifactID, Kind: "core", Platform: item.Platform,
			Architecture: item.Architecture, FileName: item.FileName, SizeBytes: item.SizeBytes,
			SHA256: item.SHA256, Signature: item.Signature,
		}},
	}
	metadataBytes, err := json.Marshal(metadata)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(releaseDir, "release-metadata.json"), metadataBytes, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := validateLocalReleaseEvidence(releaseDir, manifestBytes, release); err != nil {
		t.Fatalf("valid release evidence was rejected: %v", err)
	}
	if err := os.WriteFile(filepath.Join(releaseDir, "sbom.cdx.json"), []byte("tampered"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := validateLocalReleaseEvidence(releaseDir, manifestBytes, release); err == nil {
		t.Fatal("tampered SBOM evidence was accepted")
	}
}

func TestPreviewStateReceiptIncludesRedactedObservabilitySummary(t *testing.T) {
	payload := []byte(`{
		"created_at":"2026-08-07T15:19:05Z",
		"database_sha256":"792db9205594b63087c6492e0b51fb04c5b47032b2f4dd964e6f78b5266890e2",
		"file_count":5,
		"managed_session_cleared":true,
		"observability_rows_removed":{"observability_audit_outbox":1381},
		"schema_version":1,
		"size_bytes":14168298,
		"snapshot_id":"cf3eb63768fdaf1a742d06b937ab1eb7",
		"status":"ready"
	}`)
	var receipt previewStateReceipt
	if err := decodeExact(payload, &receipt); err != nil {
		t.Fatalf("current preview checkpoint contract was rejected: %v", err)
	}
	if receipt.ObservabilityRowsRemoved["observability_audit_outbox"] != 1381 {
		t.Fatal("redacted observability summary was not projected")
	}
	if !receipt.ManagedSessionCleared {
		t.Fatal("credential-bound managed session was not detached")
	}
}

func TestArtifactDownloadResumesAcrossSignedSources(t *testing.T) {
	payload := []byte("0123456789abcdef")
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(payload)
	item := artifact{
		ArtifactID:   "core-windows-x64",
		Platform:     "windows",
		Architecture: "x64",
		FileName:     "core.zip",
		SizeBytes:    int64(len(payload)),
		SHA256:       hex.EncodeToString(digest[:]),
	}
	release := &manifest{
		ReleaseID:   "release-stable-000000000000000000000001",
		Version:     "1.0.0",
		BuildDigest: fmt.Sprintf("%064x", 1),
		Sources: []source{
			{SourceID: "mirror", Kind: "github-cn-mirror", Priority: 0, BaseURL: "https://mirror.example/v1"},
			{SourceID: "github", Kind: "github-release", Priority: 1, BaseURL: "https://github.example/v1"},
		},
	}
	signingPayload := strings.Join([]string{
		"ecorex-artifact-v1",
		release.ReleaseID,
		release.Version,
		release.BuildDigest,
		item.ArtifactID,
		item.Platform,
		item.Architecture,
		item.FileName,
		strconv.FormatInt(item.SizeBytes, 10),
		item.SHA256,
		"",
	}, "\n")
	item.Signature = signature{
		Algorithm: "ed25519",
		KeyID:     "release-key",
		Value:     base64.StdEncoding.EncodeToString(ed25519.Sign(private, []byte(signingPayload))),
	}
	client := &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		headers := make(http.Header)
		body := payload
		status := http.StatusOK
		contentLength := int64(len(body))
		if request.URL.Host == "mirror.example" {
			if request.Header.Get("Range") != "bytes=0-15" {
				t.Fatalf("initial source was not bounded: %q", request.Header.Get("Range"))
			}
			body = payload[:5]
			contentLength = -1
			status = http.StatusPartialContent
			headers.Set("Content-Range", "bytes 0-15/16")
		} else {
			if request.Header.Get("Range") != "bytes=5-15" {
				t.Fatalf("fallback did not resume the verified partial: %q", request.Header.Get("Range"))
			}
			body = payload[5:]
			status = http.StatusPartialContent
			contentLength = int64(len(body))
			headers.Set("Content-Range", "bytes 5-15/16")
		}
		return &http.Response{
			StatusCode:    status,
			Header:        headers,
			Body:          io.NopCloser(bytes.NewReader(body)),
			ContentLength: contentLength,
			Request:       request,
		}, nil
	})}
	destination := filepath.Join(t.TempDir(), item.FileName)
	if err := downloadArtifact(
		context.Background(),
		client,
		release,
		item,
		destination,
		map[string]ed25519.PublicKey{"release-key": public},
	); err != nil {
		t.Fatal(err)
	}
	installed, err := os.ReadFile(destination)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(installed, payload) {
		t.Fatal("resumed artifact bytes differ")
	}
	if _, err := os.Lstat(destination + ".partial"); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("verified partial was not atomically committed")
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

func TestPointerAuthorityAcceptsFinalProductSemverAndRejectsDecoratedVersions(t *testing.T) {
	for _, version := range []string{"0.3.0", "1.0.0", "2.0.0"} {
		if _, err := stableReleaseSequence(version); err != nil {
			t.Fatalf("final product target was rejected: %s", version)
		}
	}
	for _, version := range []string{"01.0.0", "1.0.0-rc.1", "1.0.0+rebuilt", "10000.0.0"} {
		if _, err := stableReleaseSequence(version); err == nil {
			t.Fatalf("non-final product target was accepted: %s", version)
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
	observed, present, err := loadTrustedLocalConfig(root)
	if err != nil {
		t.Fatal(err)
	}
	if !present {
		t.Fatal("trusted local migration source was not selected")
	}
	expected, _ := filepath.Abs(legacy)
	if !samePath(observed.Source, expected) || observed.SourceVersion != "0.3.0" {
		t.Fatalf("legacy source mismatch: %#v != %q", observed, expected)
	}
	if _, err := canonicalLegacySource(root, root); err == nil {
		t.Fatal("overlapping legacy/v1 roots were accepted")
	}
}

func TestReleasedV0292InstallAndCanonicalWorkspaceAreBothRequired(t *testing.T) {
	parent := canonicalTestTempDir(t)
	root := filepath.Join(parent, "v1")
	home := filepath.Join(parent, "home")
	legacyInstall := filepath.Join(parent, "local", "EcoreX WebUI")
	runtimeRoot := filepath.Join(legacyInstall, "runtime-0.2.9.2-b909303a")
	for _, directory := range []string{
		root,
		filepath.Join(home, "EcoreX", "memory", "long-term"),
		filepath.Join(legacyInstall, "state"),
		runtimeRoot,
	} {
		if err := os.MkdirAll(directory, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	conversationDB := filepath.Join(home, "EcoreX", "memory", "long-term", "index.db")
	if err := os.WriteFile(conversationDB, []byte("sqlite-layout-evidence"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		filepath.Join(legacyInstall, "state", "current-runtime.txt"),
		[]byte(runtimeRoot+"\n"),
		0o600,
	); err != nil {
		t.Fatal(err)
	}
	manifest := []byte(`{"schemaVersion":"v0.2.5-runtime-manifest-v1","product":"EcoreX","version":"0.2.9.2","releaseGate":{"installReady":true},"ignored":"allowed"}` + "\n")
	evidence := filepath.Join(runtimeRoot, "runtime-manifest.json")
	if err := os.WriteFile(evidence, manifest, 0o600); err != nil {
		t.Fatal(err)
	}

	selection, err := discoverReleasedV0292(root, home, legacyInstall)
	if err != nil {
		t.Fatal(err)
	}
	if selection.SourceVersion != "0.2.9.2" || !samePath(selection.ReleaseEvidence, evidence) {
		t.Fatalf("released v0.2.9.2 was not selected: %#v", selection)
	}

	if err := os.Remove(conversationDB); err != nil {
		t.Fatal(err)
	}
	selection, err = discoverReleasedV0292(root, home, legacyInstall)
	if err != nil || selection.Source != "" {
		t.Fatal("release evidence without canonical workspace evidence was accepted")
	}
	if err := os.WriteFile(conversationDB, []byte("sqlite-layout-evidence"), 0o600); err != nil {
		t.Fatal(err)
	}
	wrongVersion := []byte(`{"schemaVersion":"v0.2.5-runtime-manifest-v1","product":"EcoreX","version":"0.2.9.1","releaseGate":{"installReady":true}}` + "\n")
	if err := os.WriteFile(evidence, wrongVersion, 0o600); err != nil {
		t.Fatal(err)
	}
	selection, err = discoverReleasedV0292(root, home, legacyInstall)
	if err != nil || selection.Source != "" {
		t.Fatal("non-v0.2.9.2 Runtime evidence was accepted")
	}
}

func TestCompletedMigrationSuppressesConfiguredLegacyRediscovery(t *testing.T) {
	parent := canonicalTestTempDir(t)
	root := filepath.Join(parent, "v1")
	legacy := filepath.Join(parent, "legacy")
	if err := os.MkdirAll(filepath.Join(root, "bootstrap"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "migration"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(legacy, 0o700); err != nil {
		t.Fatal(err)
	}
	configuration, err := json.Marshal(localConfig{
		SchemaVersion:       1,
		LegacySource:        legacy,
		LegacySourceVersion: "0.2.9.2",
	})
	if err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(root, "bootstrap", "bootstrap-local.json")
	if err := os.WriteFile(configPath, append(configuration, '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := hardenTestLocalConfig(configPath); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "migration", "v030-completed.json"), []byte("{}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	selection, err := selectLegacyMigration(root)
	if err != nil || selection.Source != "" {
		t.Fatalf("completed migration was selected again: %#v, %v", selection, err)
	}
}

func TestSuperviseArgumentsCarryGenericLegacyContract(t *testing.T) {
	arguments := superviseArguments(
		`C:\EcoreX`,
		[]string{"release-key=public-key"},
		legacySelection{
			Source:          `C:\Users\test\EcoreX`,
			SourceVersion:   "0.2.9.2",
			ReleaseEvidence: `C:\Legacy\runtime-manifest.json`,
		},
	)
	joined := strings.Join(arguments, "\x00")
	for _, required := range []string{
		"--legacy-source\x00C:\\Users\\test\\EcoreX",
		"--legacy-source-version\x000.2.9.2",
		"--legacy-release-evidence\x00C:\\Legacy\\runtime-manifest.json",
	} {
		if !strings.Contains(joined, required) {
			t.Fatalf("supervisor arguments omitted %q: %#v", required, arguments)
		}
	}
	if strings.Contains(joined, "--legacy-v030-source") {
		t.Fatal("generic v0.2.9.2 selection was downgraded to the v0.3-only alias")
	}
}

func TestPreviewSuperviseArgumentsUseIndependentEndpointAndMode(t *testing.T) {
	arguments := superviseArgumentsAt(
		"/tmp/ecorex-preview",
		[]string{"release-key=/tmp/release.pub"},
		legacySelection{},
		18765,
		true,
	)
	joined := strings.Join(arguments, "\x00")
	for _, required := range []string{
		"--install-root\x00/tmp/ecorex-preview",
		"--trusted-public-key\x00release-key=/tmp/release.pub",
		"--host\x00127.0.0.1\x00--port\x0018765",
		"--acceptance-preview",
	} {
		if !strings.Contains(joined, required) {
			t.Fatalf("preview arguments omitted %q: %#v", required, arguments)
		}
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

func TestRequiredBootstrapArtifactIsBoundToSignedManifestAndDiscovery(t *testing.T) {
	item := artifact{
		ArtifactID:   "bootstrap-windows-x64",
		Platform:     "windows",
		Architecture: "x64",
		FileName:     "ecorex-bootstrap-windows-x64-1.0.3.zip",
		SizeBytes:    128,
		SHA256:       strings.Repeat("a", 64),
		Signature: signature{
			Algorithm: "ed25519",
			KeyID:     "release-key",
			Value:     base64.StdEncoding.EncodeToString(make([]byte, ed25519.SignatureSize)),
		},
	}
	sources := []source{
		{SourceID: "mirror", Kind: "github-cn-mirror", Priority: 0, BaseURL: "https://mirror.example/v1"},
		{SourceID: "github", Kind: "github-release", Priority: 1, BaseURL: "https://github.example/v1"},
		{SourceID: "cdn", Kind: "ecorex-cdn", Priority: 2, BaseURL: "https://cdn.example/v1"},
	}
	discoveredSources := make([]indexSource, 0, len(sources))
	for _, origin := range sources {
		discoveredSources = append(discoveredSources, indexSource{
			SourceID: origin.SourceID,
			Kind:     origin.Kind,
			Priority: origin.Priority,
			URL:      strings.TrimRight(origin.BaseURL, "/") + "/" + item.FileName,
		})
	}
	release := &manifest{Sources: sources, Artifacts: []artifact{item}}
	discovery := &indexRelease{BootstrapArtifacts: []indexArtifact{{
		ArtifactID:   item.ArtifactID,
		Platform:     item.Platform,
		Architecture: item.Architecture,
		FileName:     item.FileName,
		SizeBytes:    item.SizeBytes,
		SHA256:       item.SHA256,
		Signature:    item.Signature,
		Sources:      discoveredSources,
	}}}
	selected, err := requiredBootstrapArtifact(release, discovery, "windows", "x64")
	if err != nil || selected != item {
		t.Fatalf("signed host Bootstrap was rejected: %v", err)
	}
	discovery.BootstrapArtifacts[0].Sources = discovery.BootstrapArtifacts[0].Sources[:1]
	if _, err := requiredBootstrapArtifact(release, discovery, "windows", "x64"); err != nil {
		t.Fatalf("relaxed single-source Stable Bootstrap was rejected: %v", err)
	}
	discovery.BootstrapArtifacts[0].Sources[0].URL = "http://mirror.example/wrong.zip"
	if _, err := requiredBootstrapArtifact(release, discovery, "windows", "x64"); err == nil {
		t.Fatal("an insecure Bootstrap discovery source was accepted")
	}
}

func TestOrphanRuntimeIsOpenedWithoutRotatingItsOwnerNonce(t *testing.T) {
	root := canonicalTestTempDir(t)
	slotID := "slot-orphan-runtime"
	releaseID := "release-stable-1234567890abcdef12345678"
	version := "1.0.0"
	if err := os.MkdirAll(filepath.Join(root, "slots", slotID), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := atomicWrite(
		filepath.Join(root, "slot-pointers.json"),
		[]byte(fmt.Sprintf(
			`{"current":%q,"previous":null,"known_good":[%q]}`+"\n",
			slotID,
			slotID,
		)),
		0o600,
	); err != nil {
		t.Fatal(err)
	}
	if err := atomicWrite(
		filepath.Join(root, "slots", slotID, ".slot.json"),
		[]byte(fmt.Sprintf(
			`{"release_id":%q,"version":%q}`+"\n",
			releaseID,
			version,
		)),
		0o600,
	); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "bootstrap"), 0o700); err != nil {
		t.Fatal(err)
	}
	nonce, err := issueRuntimeOwnerReceipt(root)
	if err != nil {
		t.Fatal(err)
	}
	receiptPath := filepath.Join(root, "bootstrap", "runtime-owner.json")
	before, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(
		response http.ResponseWriter,
		request *http.Request,
	) {
		response.Header().Set("Cache-Control", "no-store")
		switch request.URL.Path {
		case "/api/v1/runtime-owner":
			if request.Header.Get("X-EcoreX-Owner-Nonce") != "" {
				t.Fatal("Runtime owner secret was disclosed to the listener")
			}
			challenge := request.Header.Get("X-EcoreX-Owner-Challenge")
			proof, ok := runtimeOwnerProof(nonce, challenge)
			if !ok {
				http.NotFound(response, request)
				return
			}
			response.Header().Set(
				"X-EcoreX-Runtime-Owner",
				base64.RawURLEncoding.EncodeToString(proof),
			)
			response.WriteHeader(http.StatusNoContent)
		case "/":
			_, _ = fmt.Fprintf(
				response,
				`window.__ECOREX_RUNTIME__=Object.freeze({"releaseId":%q,"version":%q})`,
				releaseID,
				version,
			)
		default:
			http.NotFound(response, request)
		}
	}))
	defer server.Close()
	openCalls := 0
	opened, err := openRunningRuntimeAt(
		root,
		server.URL+"/",
		func(location string) error {
			openCalls++
			if location != server.URL+"/" {
				t.Fatalf("unexpected WebUI location: %q", location)
			}
			return nil
		},
	)
	if err != nil || !opened || openCalls != 1 {
		t.Fatalf("verified orphan Runtime was not opened: %v", err)
	}
	after, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before, after) {
		t.Fatal("orphan Runtime owner nonce was rotated before hot-open")
	}
	browserReceipt, err := os.ReadFile(filepath.Join(root, "bootstrap", "browser-opened.json"))
	if err != nil {
		t.Fatal(err)
	}
	var browserOpen map[string]any
	if err := json.Unmarshal(browserReceipt, &browserOpen); err != nil {
		t.Fatal(err)
	}
	if browserOpen["status"] != "opened" ||
		browserOpen["release_id"] != releaseID ||
		browserOpen["version"] != version ||
		browserOpen["url"] != server.URL+"/" {
		t.Fatalf("browser-open receipt is not bound to the installed Runtime: %#v", browserOpen)
	}
	if err := os.Remove(filepath.Join(root, "bootstrap", "browser-opened.json")); err != nil {
		t.Fatal(err)
	}
	opened, err = openRunningRuntimeAt(root, server.URL+"/", nil)
	if err != nil || !opened {
		t.Fatalf("verified Runtime was not detected for the desktop shell: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "bootstrap", "browser-opened.json")); !os.IsNotExist(err) {
		t.Fatal("no-open mode wrote a browser-open receipt")
	}
	if _, err := issueRuntimeOwnerReceipt(root); err != nil {
		t.Fatal(err)
	}
	openCalls = 0
	opened, err = openRunningRuntimeAt(
		root,
		server.URL+"/",
		func(_ string) error {
			openCalls++
			return nil
		},
	)
	if err != nil || opened || openCalls != 0 {
		t.Fatal("a service without the persisted owner nonce was hot-opened")
	}
}

func TestLocalInstallWaitsForLaunchOwnerInsteadOfOpeningOldRuntime(t *testing.T) {
	root := canonicalTestTempDir(t)
	path := filepath.Join(root, "bootstrap-launch.lock")
	held, err := acquireProductLock(path)
	if err != nil {
		t.Fatal(err)
	}
	released := make(chan struct{})
	go func() {
		time.Sleep(75 * time.Millisecond)
		held.close()
		close(released)
	}()

	started := time.Now()
	acquired, err := acquireLocalInstallLock(path, time.Second, 10*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer acquired.close()
	<-released
	if time.Since(started) < 50*time.Millisecond {
		t.Fatal("local install did not wait for the running Bootstrap to exit")
	}
}
