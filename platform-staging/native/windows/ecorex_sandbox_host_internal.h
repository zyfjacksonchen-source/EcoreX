#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <filesystem>
#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace ecorex::sandbox {

inline constexpr wchar_t kProtocol[] = L"ecorex-sandbox-launch-v1";
inline constexpr wchar_t kContainerPrefix[] = L"EcoreX.Sandbox.v1.";
inline constexpr SIZE_T kProcessMemoryLimit = 512ull * 1024 * 1024;
inline constexpr SIZE_T kJobMemoryLimit = 768ull * 1024 * 1024;
inline constexpr DWORD kCpuRate = 8000;

struct LocalFreeDeleter {
  void operator()(void* value) const {
    if (value != nullptr) LocalFree(value);
  }
};
using LocalPointer = std::unique_ptr<void, LocalFreeDeleter>;

struct SecurityRoots {
  std::filesystem::path install_root;
  std::filesystem::path slot_root;
  std::wstring slot_digest;
  std::wstring workspace_digest;
  std::vector<std::filesystem::path> read_roots;
  std::vector<std::filesystem::path> workspaces;
  std::wstring mode = L"full";
};

std::wstring Quote(const std::wstring& value);
std::filesystem::path ModulePath();
std::optional<std::filesystem::path> AbsoluteRegular(const std::wstring& raw);
std::optional<std::filesystem::path> AbsoluteDirectory(const std::wstring& raw);
std::string Utf8(const std::wstring& value);
std::optional<std::string> Sha256File(const std::filesystem::path& path);
std::optional<std::string> RootsDigest(
    const std::vector<std::filesystem::path>& roots);
PSID AppContainerSid(const std::wstring& workspace_digest);
bool IsSha256(const std::wstring& value);
std::optional<std::string> PermissionDomainDigest(
    const std::vector<std::filesystem::path>& roots);
bool ContainedPath(const std::filesystem::path& candidate,
                   const std::filesystem::path& root);
bool ValidateSecurityRoots(const SecurityRoots& request);
bool AttestSecurity(const SecurityRoots& request, PSID sid, bool full,
                    std::string* digest, std::string* failure = nullptr,
                    bool strict_children = false);
bool PrepareProbeWorkspace(const std::filesystem::path& workspace, PSID sid);

int SecurityCommand(const std::wstring& operation, int argc, wchar_t** argv);
int ProbeChild(const std::filesystem::path& workspace,
               const std::filesystem::path& outside, unsigned short port);
int Probe(const std::wstring& expected_digest,
          const std::vector<std::filesystem::path>& workspaces);
int Run(int argc, wchar_t** argv);

}  // namespace ecorex::sandbox
