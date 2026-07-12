// AppContainer identity, filesystem provisioning, and attestation.
#include "ecorex_sandbox_host_internal.h"

#include <aclapi.h>
#include <bcrypt.h>
#include <sddl.h>
#include <userenv.h>

#include <algorithm>
#include <array>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <set>
#include <sstream>

namespace ecorex::sandbox {

std::wstring Quote(const std::wstring& value) {
  std::wstring out = L"\"";
  unsigned slashes = 0;
  for (const wchar_t ch : value) {
    if (ch == L'\\') {
      ++slashes;
    } else if (ch == L'\"') {
      out.append(slashes * 2 + 1, L'\\');
      out.push_back(ch);
      slashes = 0;
    } else {
      out.append(slashes, L'\\');
      slashes = 0;
      out.push_back(ch);
    }
  }
  out.append(slashes * 2, L'\\');
  out.push_back(L'\"');
  return out;
}

std::filesystem::path ModulePath() {
  std::vector<wchar_t> buffer(32768);
  const DWORD size = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
  if (size == 0 || size >= static_cast<DWORD>(buffer.size())) return {};
  return std::filesystem::path(std::wstring(buffer.data(), size));
}

bool RealPathComponents(const std::filesystem::path& value) {
  if (!value.is_absolute()) return false;
  std::filesystem::path current = value.root_path();
  for (const auto& component : value.relative_path()) {
    if (component == L"." || component == L".." || component.empty()) return false;
    current /= component;
    const DWORD attributes = GetFileAttributesW(current.c_str());
    if (attributes == INVALID_FILE_ATTRIBUTES ||
        (attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0) {
      return false;
    }
  }
  return true;
}

std::optional<std::filesystem::path> AbsoluteRegular(const std::wstring& raw) {
  std::error_code error;
  std::filesystem::path value(raw);
  if (!RealPathComponents(value)) return std::nullopt;
  const auto canonical = std::filesystem::canonical(value, error);
  if (error || !std::filesystem::is_regular_file(canonical, error)) return std::nullopt;
  return canonical;
}

std::optional<std::filesystem::path> AbsoluteDirectory(const std::wstring& raw) {
  std::error_code error;
  std::filesystem::path value(raw);
  if (!RealPathComponents(value)) return std::nullopt;
  const auto canonical = std::filesystem::canonical(value, error);
  if (error || !std::filesystem::is_directory(canonical, error)) return std::nullopt;
  return canonical;
}

std::string Utf8(const std::wstring& value) {
  if (value.empty()) return {};
  const int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
                                      static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
  if (size <= 0) return {};
  std::string result(static_cast<size_t>(size), '\0');
  if (WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
                          static_cast<int>(value.size()), result.data(), size, nullptr, nullptr) != size) {
    return {};
  }
  return result;
}

std::optional<std::string> Sha256Bytes(const unsigned char* data, size_t size) {
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_size = 0;
  DWORD result_size = 0;
  if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) != 0 ||
      BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                        reinterpret_cast<PUCHAR>(&object_size), sizeof(object_size),
                        &result_size, 0) != 0) {
    if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
    return std::nullopt;
  }
  std::vector<unsigned char> object(object_size);
  std::array<unsigned char, 32> digest{};
  bool ok = BCryptCreateHash(algorithm, &hash, object.data(), object_size, nullptr, 0, 0) == 0 &&
            BCryptHashData(hash, const_cast<PUCHAR>(data), static_cast<ULONG>(size), 0) == 0 &&
            BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0) == 0;
  if (hash) BCryptDestroyHash(hash);
  BCryptCloseAlgorithmProvider(algorithm, 0);
  if (!ok) return std::nullopt;
  std::ostringstream text;
  text << std::hex << std::setfill('0');
  for (const auto byte : digest) text << std::setw(2) << static_cast<unsigned>(byte);
  return text.str();
}

std::optional<std::string> Sha256File(const std::filesystem::path& path) {
  HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
  if (file == INVALID_HANDLE_VALUE) return std::nullopt;
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  BCRYPT_HASH_HANDLE hash = nullptr;
  DWORD object_size = 0;
  DWORD result_size = 0;
  bool ok = BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) == 0 &&
            BCryptGetProperty(algorithm, BCRYPT_OBJECT_LENGTH,
                              reinterpret_cast<PUCHAR>(&object_size), sizeof(object_size),
                              &result_size, 0) == 0;
  std::vector<unsigned char> object(object_size);
  if (ok) ok = BCryptCreateHash(algorithm, &hash, object.data(), object_size, nullptr, 0, 0) == 0;
  std::array<unsigned char, 64 * 1024> buffer{};
  while (ok) {
    DWORD read = 0;
    if (!ReadFile(file, buffer.data(), static_cast<DWORD>(buffer.size()), &read, nullptr)) {
      ok = false;
      break;
    }
    if (read == 0) break;
    ok = BCryptHashData(hash, buffer.data(), read, 0) == 0;
  }
  std::array<unsigned char, 32> digest{};
  if (ok) ok = BCryptFinishHash(hash, digest.data(), static_cast<ULONG>(digest.size()), 0) == 0;
  if (hash) BCryptDestroyHash(hash);
  if (algorithm) BCryptCloseAlgorithmProvider(algorithm, 0);
  CloseHandle(file);
  if (!ok) return std::nullopt;
  std::ostringstream text;
  text << std::hex << std::setfill('0');
  for (const auto byte : digest) text << std::setw(2) << static_cast<unsigned>(byte);
  return text.str();
}

std::optional<std::string> RootsDigest(const std::vector<std::filesystem::path>& roots) {
  std::string bytes;
  for (size_t index = 0; index < roots.size(); ++index) {
    if (index) bytes.push_back('\0');
    const auto encoded = Utf8(roots[index].wstring());
    if (encoded.empty()) return std::nullopt;
    bytes += encoded;
  }
  return Sha256Bytes(reinterpret_cast<const unsigned char*>(bytes.data()), bytes.size());
}

bool IsSha256(const std::wstring& value);

std::optional<std::wstring> AppContainerName(const std::wstring& workspace_digest) {
  if (!IsSha256(workspace_digest)) return std::nullopt;
  return std::wstring(kContainerPrefix) + workspace_digest.substr(0, 32);
}

PSID AppContainerSid(const std::wstring& workspace_digest) {
  const auto container_name = AppContainerName(workspace_digest);
  if (!container_name) return nullptr;
  PSID sid = nullptr;
  const HRESULT created = CreateAppContainerProfile(
      container_name->c_str(), L"EcoreX Sandbox", L"EcoreX workspace permission domain",
      nullptr, 0, &sid);
  if (SUCCEEDED(created)) return sid;
  if (created != HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS) ||
      FAILED(DeriveAppContainerSidFromAppContainerName(container_name->c_str(), &sid))) {
    return nullptr;
  }
  return sid;
}

bool IsSha256(const std::wstring& value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](wchar_t character) {
           return (character >= L'0' && character <= L'9') ||
                  (character >= L'a' && character <= L'f');
         });
}

std::optional<std::wstring> SidString(PSID sid) {
  LPWSTR value = nullptr;
  if (sid == nullptr || !ConvertSidToStringSidW(sid, &value)) return std::nullopt;
  LocalPointer owned(value);
  return std::wstring(value);
}

std::wstring FoldPath(const std::filesystem::path& path) {
  std::wstring value = path.lexically_normal().wstring();
  while (value.size() > 3 && (value.back() == L'\\' || value.back() == L'/')) {
    value.pop_back();
  }
  if (value.empty() ||
      value.size() > static_cast<size_t>((std::numeric_limits<int>::max)())) {
    return {};
  }
  const int required = LCMapStringEx(
      LOCALE_NAME_INVARIANT, LCMAP_LOWERCASE, value.data(),
      static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr, 0);
  if (required <= 0) return {};
  std::wstring folded(static_cast<size_t>(required), L'\0');
  const int written = LCMapStringEx(
      LOCALE_NAME_INVARIANT, LCMAP_LOWERCASE, value.data(),
      static_cast<int>(value.size()), folded.data(), required, nullptr, nullptr, 0);
  if (written != required) return {};
  return folded;
}

std::optional<std::string> PermissionDomainDigest(
    const std::vector<std::filesystem::path>& roots) {
  std::vector<std::wstring> normalized;
  normalized.reserve(roots.size());
  for (const auto& root : roots) normalized.push_back(FoldPath(root));
  std::sort(normalized.begin(), normalized.end());
  if (std::adjacent_find(normalized.begin(), normalized.end()) != normalized.end()) {
    return std::nullopt;
  }
  std::string bytes;
  for (size_t index = 0; index < normalized.size(); ++index) {
    if (index) bytes.push_back('\0');
    const auto encoded = Utf8(normalized[index]);
    if (encoded.empty()) return std::nullopt;
    bytes += encoded;
  }
  return Sha256Bytes(reinterpret_cast<const unsigned char*>(bytes.data()), bytes.size());
}

std::optional<std::string> RelativeRootsDigest(
    const std::vector<std::filesystem::path>& roots,
    const std::filesystem::path& base) {
  std::vector<std::wstring> normalized;
  normalized.reserve(roots.size());
  for (const auto& root : roots) {
    std::error_code error;
    const auto relative = std::filesystem::relative(root, base, error);
    if (error || relative.empty() || relative.is_absolute()) return std::nullopt;
    normalized.push_back(FoldPath(relative));
  }
  std::sort(normalized.begin(), normalized.end());
  if (std::adjacent_find(normalized.begin(), normalized.end()) != normalized.end()) {
    return std::nullopt;
  }
  std::string bytes;
  for (size_t index = 0; index < normalized.size(); ++index) {
    if (index) bytes.push_back('\0');
    const auto encoded = Utf8(normalized[index]);
    if (encoded.empty()) return std::nullopt;
    bytes += encoded;
  }
  return Sha256Bytes(reinterpret_cast<const unsigned char*>(bytes.data()), bytes.size());
}

bool ContainedPath(const std::filesystem::path& candidate,
                   const std::filesystem::path& root) {
  const auto child = FoldPath(candidate);
  const auto parent = FoldPath(root);
  return !child.empty() && !parent.empty() && (child == parent ||
         (child.size() > parent.size() && child.compare(0, parent.size(), parent) == 0 &&
          (child[parent.size()] == L'\\' || child[parent.size()] == L'/')));
}

bool ReparsePoint(const std::filesystem::path& path) {
  const DWORD attributes = GetFileAttributesW(path.c_str());
  return attributes == INVALID_FILE_ATTRIBUTES ||
         (attributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0;
}

bool EnumerateSecureTree(const std::filesystem::path& root,
                         std::vector<std::filesystem::path>* nodes) {
  if (nodes == nullptr || ReparsePoint(root)) return false;
  nodes->push_back(root);
  std::error_code error;
  std::filesystem::recursive_directory_iterator iterator(
      root, std::filesystem::directory_options::none, error);
  const std::filesystem::recursive_directory_iterator end;
  while (!error && iterator != end) {
    const auto path = iterator->path();
    if (ReparsePoint(path)) return false;
    const auto status = iterator->symlink_status(error);
    if (error || (!std::filesystem::is_regular_file(status) &&
                  !std::filesystem::is_directory(status))) {
      return false;
    }
    nodes->push_back(path);
    iterator.increment(error);
  }
  if (error) return false;
  std::sort(nodes->begin(), nodes->end(), [](const auto& left, const auto& right) {
    return FoldPath(left) < FoldPath(right);
  });
  return true;
}

bool ApplyGrant(const std::filesystem::path& path, PSID sid, DWORD permissions,
                bool directory) {
  PACL old_acl = nullptr;
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  if (GetNamedSecurityInfoW(const_cast<LPWSTR>(path.c_str()), SE_FILE_OBJECT,
                            DACL_SECURITY_INFORMATION, nullptr, nullptr, &old_acl,
                            nullptr, &descriptor) != ERROR_SUCCESS) {
    return false;
  }
  LocalPointer owned_descriptor(descriptor);
  EXPLICIT_ACCESSW access{};
  access.grfAccessPermissions = permissions;
  access.grfAccessMode = GRANT_ACCESS;
  access.grfInheritance = directory ? SUB_CONTAINERS_AND_OBJECTS_INHERIT : NO_INHERITANCE;
  access.Trustee.TrusteeForm = TRUSTEE_IS_SID;
  access.Trustee.TrusteeType = TRUSTEE_IS_USER;
  access.Trustee.ptstrName = static_cast<LPWSTR>(sid);
  PACL updated = nullptr;
  if (SetEntriesInAclW(1, &access, old_acl, &updated) != ERROR_SUCCESS) return false;
  LocalPointer owned_updated(updated);
  return SetNamedSecurityInfoW(const_cast<LPWSTR>(path.c_str()), SE_FILE_OBJECT,
                               DACL_SECURITY_INFORMATION, nullptr, nullptr, updated,
                               nullptr) == ERROR_SUCCESS;
}

bool RemoveGrant(const std::filesystem::path& path, PSID sid) {
  PACL old_acl = nullptr;
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  if (GetNamedSecurityInfoW(const_cast<LPWSTR>(path.c_str()), SE_FILE_OBJECT,
                            DACL_SECURITY_INFORMATION, nullptr, nullptr, &old_acl,
                            nullptr, &descriptor) != ERROR_SUCCESS) {
    return false;
  }
  LocalPointer owned_descriptor(descriptor);
  EXPLICIT_ACCESSW access{};
  access.grfAccessMode = REVOKE_ACCESS;
  access.grfInheritance = NO_INHERITANCE;
  access.Trustee.TrusteeForm = TRUSTEE_IS_SID;
  access.Trustee.TrusteeType = TRUSTEE_IS_USER;
  access.Trustee.ptstrName = static_cast<LPWSTR>(sid);
  PACL updated = nullptr;
  if (SetEntriesInAclW(1, &access, old_acl, &updated) != ERROR_SUCCESS) return false;
  LocalPointer owned_updated(updated);
  return SetNamedSecurityInfoW(const_cast<LPWSTR>(path.c_str()), SE_FILE_OBJECT,
                               DACL_SECURITY_INFORMATION, nullptr, nullptr, updated,
                               nullptr) == ERROR_SUCCESS;
}

bool ApplyLowIntegrity(const std::filesystem::path& path, bool directory) {
  const wchar_t* sddl = directory ? L"S:(ML;OICI;NW;;;LW)" : L"S:(ML;;NW;;;LW)";
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
          sddl, SDDL_REVISION_1, &descriptor, nullptr)) {
    return false;
  }
  LocalPointer owned_descriptor(descriptor);
  PACL label = nullptr;
  BOOL present = FALSE;
  BOOL defaulted = FALSE;
  if (!GetSecurityDescriptorSacl(descriptor, &present, &label, &defaulted) || !present) {
    return false;
  }
  return SetNamedSecurityInfoW(const_cast<LPWSTR>(path.c_str()), SE_FILE_OBJECT,
                               LABEL_SECURITY_INFORMATION, nullptr, nullptr, nullptr,
                               label) == ERROR_SUCCESS;
}

bool RemoveIntegrityLabel(const std::filesystem::path& path) {
  std::array<unsigned char, sizeof(ACL)> storage{};
  auto* empty = reinterpret_cast<PACL>(storage.data());
  if (!InitializeAcl(empty, static_cast<DWORD>(storage.size()), ACL_REVISION)) return false;
  return SetNamedSecurityInfoW(const_cast<LPWSTR>(path.c_str()), SE_FILE_OBJECT,
                               LABEL_SECURITY_INFORMATION, nullptr, nullptr, nullptr,
                               empty) == ERROR_SUCCESS;
}

bool GrantsPermissions(const std::filesystem::path& path, PSID sid,
                       DWORD permissions, bool require_inheritance) {
  PACL acl = nullptr;
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  if (GetNamedSecurityInfoW(const_cast<LPWSTR>(path.c_str()), SE_FILE_OBJECT,
                            DACL_SECURITY_INFORMATION, nullptr, nullptr, &acl,
                            nullptr, &descriptor) != ERROR_SUCCESS || acl == nullptr) {
    if (descriptor) LocalFree(descriptor);
    return false;
  }
  LocalPointer owned_descriptor(descriptor);
  GENERIC_MAPPING mapping{
      FILE_GENERIC_READ, FILE_GENERIC_WRITE, FILE_GENERIC_EXECUTE, FILE_ALL_ACCESS};
  DWORD required = permissions;
  MapGenericMask(&required, &mapping);
  DWORD granted = 0;
  bool inheritance = !require_inheritance;
  for (DWORD index = 0; index < acl->AceCount; ++index) {
    void* raw = nullptr;
    if (!GetAce(acl, index, &raw)) return false;
    const auto* header = static_cast<const ACE_HEADER*>(raw);
    if (header->AceType != ACCESS_ALLOWED_ACE_TYPE &&
        header->AceType != ACCESS_DENIED_ACE_TYPE) {
      continue;
    }
    const auto* ace = static_cast<const ACCESS_ALLOWED_ACE*>(raw);
    PSID ace_sid = const_cast<DWORD*>(&ace->SidStart);
    if (!EqualSid(ace_sid, sid)) continue;
    DWORD mask = ace->Mask;
    MapGenericMask(&mask, &mapping);
    if (header->AceType == ACCESS_DENIED_ACE_TYPE &&
        (mask & required) != 0) {
      return false;
    }
    if (header->AceType == ACCESS_ALLOWED_ACE_TYPE) {
      // This ACE is an exact capability boundary, not a minimum grant.
      // Read roots in particular must reject write/delete/owner/DACL rights.
      if ((mask & ~required) != 0) return false;
      if ((header->AceFlags & INHERIT_ONLY_ACE) == 0) granted |= mask;
      if ((header->AceFlags & (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)) ==
          (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)) {
        inheritance = true;
      }
    }
  }
  return (granted & required) == required && inheritance;
}

bool HasLowIntegrity(const std::filesystem::path& path, bool require_inheritance) {
  PACL label = nullptr;
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  if (GetNamedSecurityInfoW(const_cast<LPWSTR>(path.c_str()), SE_FILE_OBJECT,
                            LABEL_SECURITY_INFORMATION, nullptr, nullptr, nullptr,
                            &label, &descriptor) != ERROR_SUCCESS || label == nullptr) {
    if (descriptor) LocalFree(descriptor);
    return false;
  }
  LocalPointer owned_descriptor(descriptor);
  for (DWORD index = 0; index < label->AceCount; ++index) {
    void* raw = nullptr;
    if (!GetAce(label, index, &raw)) return false;
    const auto* header = static_cast<const ACE_HEADER*>(raw);
    if (header->AceType != SYSTEM_MANDATORY_LABEL_ACE_TYPE) continue;
    const auto* ace = static_cast<const SYSTEM_MANDATORY_LABEL_ACE*>(raw);
    PSID integrity_sid = const_cast<DWORD*>(&ace->SidStart);
    const DWORD count = *GetSidSubAuthorityCount(integrity_sid);
    if (count == 0) continue;
    const DWORD level = *GetSidSubAuthority(integrity_sid, count - 1);
    const bool inheritance =
        !require_inheritance ||
        (header->AceFlags & (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE)) ==
            (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE);
    if (level == SECURITY_MANDATORY_LOW_RID &&
        (ace->Mask & SYSTEM_MANDATORY_LABEL_NO_WRITE_UP) != 0 && inheritance) {
      return true;
    }
  }
  return false;
}

std::optional<std::string> SecurityDescriptorText(const std::filesystem::path& path) {
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  PACL dacl = nullptr;
  PACL label = nullptr;
  if (GetNamedSecurityInfoW(const_cast<LPWSTR>(path.c_str()), SE_FILE_OBJECT,
                            DACL_SECURITY_INFORMATION | LABEL_SECURITY_INFORMATION,
                            nullptr, nullptr, &dacl, &label, &descriptor) != ERROR_SUCCESS) {
    return std::nullopt;
  }
  LocalPointer owned_descriptor(descriptor);
  LPWSTR value = nullptr;
  if (!ConvertSecurityDescriptorToStringSecurityDescriptorW(
          descriptor, SDDL_REVISION_1,
          DACL_SECURITY_INFORMATION | LABEL_SECURITY_INFORMATION, &value,
          nullptr)) {
    return std::nullopt;
  }
  LocalPointer owned_value(value);
  return Utf8(value);
}

std::optional<std::string> RootFileIdentity(const std::filesystem::path& path) {
  HANDLE handle = CreateFileW(
      path.c_str(), FILE_READ_ATTRIBUTES,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr, OPEN_EXISTING,
      FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
  if (handle == INVALID_HANDLE_VALUE) return std::nullopt;
  BY_HANDLE_FILE_INFORMATION information{};
  const bool ok = GetFileInformationByHandle(handle, &information) &&
                  (information.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) == 0;
  CloseHandle(handle);
  if (!ok) return std::nullopt;
  std::ostringstream value;
  value << std::hex << information.dwVolumeSerialNumber << ':'
        << information.nFileIndexHigh << ':' << information.nFileIndexLow;
  return value.str();
}

bool ValidateSecurityRoots(const SecurityRoots& request) {
  std::error_code canonical_error;
  const auto slots_root = std::filesystem::canonical(
      request.install_root / L"slots", canonical_error);
  if (canonical_error) return false;
  const auto managed_workspace = std::filesystem::canonical(
      request.install_root / L"workspace", canonical_error);
  if (canonical_error || ReparsePoint(slots_root) || ReparsePoint(managed_workspace)) {
    return false;
  }
  if (!IsSha256(request.slot_digest) || !IsSha256(request.workspace_digest) ||
      request.read_roots.empty() || request.workspaces.empty() ||
      !ContainedPath(request.slot_root, slots_root) ||
      FoldPath(request.slot_root) == FoldPath(slots_root) ||
      FoldPath(request.slot_root.parent_path()) != FoldPath(slots_root)) {
    return false;
  }
  const auto workspace_digest = RootsDigest(request.workspaces);
  if (!workspace_digest || Utf8(request.workspace_digest) != *workspace_digest) return false;
  std::set<std::wstring> identities;
  for (const auto& root : request.read_roots) {
    if (!ContainedPath(root, request.slot_root) || ReparsePoint(root) ||
        !identities.insert(FoldPath(root)).second) {
      return false;
    }
  }
  for (size_t left = 0; left < request.read_roots.size(); ++left) {
    for (size_t right = left + 1; right < request.read_roots.size(); ++right) {
      if (ContainedPath(request.read_roots[left], request.read_roots[right]) ||
          ContainedPath(request.read_roots[right], request.read_roots[left])) {
        return false;
      }
    }
  }
  for (const auto& root : request.workspaces) {
    if (!ContainedPath(root, managed_workspace) || ContainedPath(root, request.slot_root) ||
        ReparsePoint(root) || !identities.insert(FoldPath(root)).second) {
      return false;
    }
  }
  for (size_t left = 0; left < request.workspaces.size(); ++left) {
    for (size_t right = left + 1; right < request.workspaces.size(); ++right) {
      if (ContainedPath(request.workspaces[left], request.workspaces[right]) ||
          ContainedPath(request.workspaces[right], request.workspaces[left])) {
        return false;
      }
    }
  }
  return true;
}

bool TrustedHelperLocation(const SecurityRoots& request, bool bootstrap_only) {
  const auto module = AbsoluteRegular(ModulePath().wstring());
  if (!module || ReparsePoint(*module)) return false;
  std::error_code error;
  const auto bootstrap = std::filesystem::canonical(
      request.install_root / L"bootstrap", error);
  if (error || ReparsePoint(bootstrap)) return false;
  if (bootstrap_only) {
    return ContainedPath(*module, bootstrap) &&
           !ContainedPath(*module, request.slot_root);
  }
  return ContainedPath(*module, request.slot_root) ||
         ContainedPath(*module, bootstrap);
}

bool CollectSecurityNodes(const SecurityRoots& request, bool full,
                          std::vector<std::filesystem::path>* read_nodes,
                          std::vector<std::filesystem::path>* workspace_nodes) {
  for (const auto& root : request.read_roots) {
    if (full) {
      std::vector<std::filesystem::path> tree;
      if (!EnumerateSecureTree(root, &tree)) return false;
      read_nodes->insert(read_nodes->end(), tree.begin(), tree.end());
    } else {
      read_nodes->push_back(root);
    }
  }
  for (const auto& root : request.workspaces) {
    if (full) {
      std::vector<std::filesystem::path> tree;
      if (!EnumerateSecureTree(root, &tree)) return false;
      workspace_nodes->insert(workspace_nodes->end(), tree.begin(), tree.end());
    } else {
      workspace_nodes->push_back(root);
    }
  }
  const auto by_path = [](const auto& left, const auto& right) {
    return FoldPath(left) < FoldPath(right);
  };
  std::sort(read_nodes->begin(), read_nodes->end(), by_path);
  std::sort(workspace_nodes->begin(), workspace_nodes->end(), by_path);
  return true;
}

bool AttestSecurity(const SecurityRoots& request, PSID sid, bool full,
                    std::string* digest, std::string* failure,
                    bool strict_children) {
  std::vector<std::filesystem::path> read_nodes;
  std::vector<std::filesystem::path> workspace_nodes;
  if (!CollectSecurityNodes(request, full, &read_nodes, &workspace_nodes)) {
    if (failure) *failure = "tree";
    return false;
  }
  std::set<std::wstring> root_identities;
  for (const auto& root : request.read_roots) root_identities.insert(FoldPath(root));
  for (const auto& root : request.workspaces) root_identities.insert(FoldPath(root));
  std::string records;
  const auto record_key = [&request](const std::filesystem::path& path,
                                     char kind) -> std::optional<std::wstring> {
    const auto& roots = kind == 'r' ? request.read_roots : request.workspaces;
    const auto& base = kind == 'r' ? request.slot_root : request.install_root;
    for (const auto& root : roots) {
      if (!ContainedPath(path, root)) continue;
      std::error_code root_error;
      std::error_code child_error;
      const auto root_relative = std::filesystem::relative(root, base, root_error);
      const auto child_relative = std::filesystem::relative(path, root, child_error);
      if (root_error || child_error || root_relative.is_absolute() ||
          child_relative.is_absolute()) {
        return std::nullopt;
      }
      return std::wstring(1, static_cast<wchar_t>(kind)) + L":" +
             FoldPath(root_relative) + L":" + FoldPath(child_relative);
    }
    return std::nullopt;
  };
  const auto append = [&records, &root_identities, &record_key](
                          const std::filesystem::path& path, char kind) -> bool {
    const auto key = record_key(path, kind);
    const auto encoded = key ? Utf8(*key) : std::string{};
    if (encoded.empty()) return false;
    const bool root = root_identities.contains(FoldPath(path));
    // Runtime payload members are immutable release material, so their names
    // remain part of the attested tree digest. Workspace members are mutable
    // user data: enumerate and validate every node below, but bind only the
    // stable workspace root identity/security descriptor into the receipt.
    // Otherwise creating a legitimate output changes the release marker and
    // makes the next process start indistinguishable from ACL tampering.
    if (kind == 'w' && !root) return true;
    records.push_back(kind);
    records.append(encoded);
    if (root) {
      const auto descriptor = SecurityDescriptorText(path);
      const auto identity = RootFileIdentity(path);
      if (!descriptor || !identity) return false;
      records.push_back('\0');
      records.append(*identity);
      records.push_back('\0');
      records.append(*descriptor);
    }
    records.push_back('\n');
    return true;
  };
  for (const auto& path : read_nodes) {
    std::error_code error;
    const bool directory = std::filesystem::is_directory(path, error);
    const bool root = strict_children || root_identities.contains(FoldPath(path));
    if (error) {
      if (failure) *failure = "read_metadata";
      return false;
    }
    if (root && !GrantsPermissions(
                    path, sid, GENERIC_READ | GENERIC_EXECUTE, directory)) {
      if (failure) *failure = "read_acl";
      return false;
    }
    if (!append(path, 'r')) {
      if (failure) *failure = "read_identity";
      return false;
    }
  }
  for (const auto& path : workspace_nodes) {
    std::error_code error;
    const bool directory = std::filesystem::is_directory(path, error);
    const bool root = strict_children || root_identities.contains(FoldPath(path));
    if (error) {
      if (failure) *failure = "workspace_metadata";
      return false;
    }
    if (root && !GrantsPermissions(
                    path, sid,
                    GENERIC_READ | GENERIC_WRITE | GENERIC_EXECUTE | DELETE,
                    directory)) {
      if (failure) *failure = "workspace_acl";
      return false;
    }
    if (root && !HasLowIntegrity(path, directory)) {
      if (failure) *failure = "workspace_label";
      return false;
    }
    if (!append(path, 'w')) {
      if (failure) *failure = "workspace_identity";
      return false;
    }
  }
  const auto hashed = Sha256Bytes(
      reinterpret_cast<const unsigned char*>(records.data()), records.size());
  if (!hashed) {
    if (failure) *failure = "digest";
    return false;
  }
  *digest = *hashed;
  return true;
}

bool ProvisionSecurity(const SecurityRoots& request, PSID sid) {
  std::vector<std::filesystem::path> read_nodes;
  std::vector<std::filesystem::path> workspace_nodes;
  if (!CollectSecurityNodes(request, false, &read_nodes, &workspace_nodes)) return false;
  for (const auto& path : read_nodes) {
    std::error_code scan_error;
    size_t existing = 0;
    for (std::filesystem::directory_iterator iterator(path, scan_error), end;
         !scan_error && iterator != end; iterator.increment(scan_error)) {
      ++existing;
      if (FoldPath(iterator->path()) != FoldPath(ModulePath()) ||
          ReparsePoint(iterator->path()) || !iterator->is_regular_file(scan_error)) {
        return false;
      }
    }
    if (scan_error || existing > 1) return false;
    std::error_code error;
    const bool directory = std::filesystem::is_directory(path, error);
    if (error || !ApplyGrant(path, sid, GENERIC_READ | GENERIC_EXECUTE, directory)) {
      return false;
    }
  }
  for (const auto& path : workspace_nodes) {
    std::error_code error;
    const bool directory = std::filesystem::is_directory(path, error);
    if (error ||
        !ApplyGrant(path, sid,
                    GENERIC_READ | GENERIC_WRITE | GENERIC_EXECUTE | DELETE,
                    directory) ||
        !ApplyLowIntegrity(path, directory)) {
      return false;
    }
  }
  return true;
}

bool RepairSecurity(const SecurityRoots& request, PSID sid) {
  std::vector<std::filesystem::path> read_nodes;
  std::vector<std::filesystem::path> workspace_nodes;
  if (!CollectSecurityNodes(request, true, &read_nodes, &workspace_nodes)) return false;
  for (const auto& path : read_nodes) {
    std::error_code error;
    const bool directory = std::filesystem::is_directory(path, error);
    if (error) return false;
    if (!GrantsPermissions(path, sid, GENERIC_READ | GENERIC_EXECUTE, directory) &&
        !ApplyGrant(path, sid, GENERIC_READ | GENERIC_EXECUTE, directory)) {
      return false;
    }
  }
  for (const auto& path : workspace_nodes) {
    std::error_code error;
    const bool directory = std::filesystem::is_directory(path, error);
    if (error) return false;
    if (!GrantsPermissions(path, sid,
                           GENERIC_READ | GENERIC_WRITE | GENERIC_EXECUTE | DELETE,
                           directory) &&
        !ApplyGrant(path, sid,
                    GENERIC_READ | GENERIC_WRITE | GENERIC_EXECUTE | DELETE,
                    directory)) {
      return false;
    }
    if (!HasLowIntegrity(path, directory) && !ApplyLowIntegrity(path, directory)) {
      return false;
    }
  }
  return true;
}

bool UnprovisionSlotSecurity(const SecurityRoots& request, PSID sid) {
  std::vector<std::filesystem::path> read_nodes;
  std::vector<std::filesystem::path> workspace_nodes;
  if (!CollectSecurityNodes(request, true, &read_nodes, &workspace_nodes)) return false;
  std::reverse(read_nodes.begin(), read_nodes.end());
  for (const auto& path : read_nodes) {
    if (!RemoveGrant(path, sid)) return false;
  }
  return true;
}

bool UnprovisionDomainSecurity(const SecurityRoots& request, PSID sid) {
  std::vector<std::filesystem::path> read_nodes;
  std::vector<std::filesystem::path> workspace_nodes;
  if (!CollectSecurityNodes(request, true, &read_nodes, &workspace_nodes)) return false;
  std::reverse(workspace_nodes.begin(), workspace_nodes.end());
  for (const auto& path : workspace_nodes) {
    if (!RemoveGrant(path, sid) || !RemoveIntegrityLabel(path)) return false;
  }
  return true;
}

std::optional<SecurityRoots> ParseSecurityRoots(int argc, wchar_t** argv,
                                                int start_index) {
  SecurityRoots request;
  bool protocol = false;
  bool install_root = false;
  bool slot_root = false;
  bool slot_digest = false;
  bool workspace_digest = false;
  bool mode = false;
  for (int index = start_index; index < argc; ++index) {
    if (index + 1 >= argc) return std::nullopt;
    const std::wstring key = argv[index];
    const std::wstring value = argv[++index];
    if (key == L"--protocol") {
      if (protocol || value != kProtocol) return std::nullopt;
      protocol = true;
    } else if (key == L"--install-root") {
      if (install_root) return std::nullopt;
      const auto root = AbsoluteDirectory(value);
      if (!root) return std::nullopt;
      request.install_root = *root;
      install_root = true;
    } else if (key == L"--slot-root") {
      if (slot_root) return std::nullopt;
      const auto root = AbsoluteDirectory(value);
      if (!root) return std::nullopt;
      request.slot_root = *root;
      slot_root = true;
    } else if (key == L"--slot-digest") {
      if (slot_digest || !IsSha256(value)) return std::nullopt;
      request.slot_digest = value;
      slot_digest = true;
    } else if (key == L"--workspace-digest") {
      if (workspace_digest || !IsSha256(value)) return std::nullopt;
      request.workspace_digest = value;
      workspace_digest = true;
    } else if (key == L"--read-root") {
      const auto root = AbsoluteDirectory(value);
      if (!root) return std::nullopt;
      request.read_roots.push_back(*root);
    } else if (key == L"--workspace") {
      const auto root = AbsoluteDirectory(value);
      if (!root) return std::nullopt;
      request.workspaces.push_back(*root);
    } else if (key == L"--mode") {
      if (mode ||
          (value != L"roots" && value != L"full" && value != L"strict")) {
        return std::nullopt;
      }
      request.mode = value;
      mode = true;
    } else {
      return std::nullopt;
    }
  }
  if (!protocol || !install_root || !slot_root || !slot_digest ||
      !workspace_digest || !ValidateSecurityRoots(request)) {
    return std::nullopt;
  }
  return request;
}

int SecurityCommand(const std::wstring& operation, int argc, wchar_t** argv) {
  const auto request = ParseSecurityRoots(argc, argv, 2);
  if (!request ||
      !TrustedHelperLocation(
          *request,
          operation == L"provision" || operation == L"unprovision-domain") ||
      (operation != L"attest" && request->mode != L"full")) {
    return 64;
  }
  const auto permission_domain = PermissionDomainDigest(request->workspaces);
  PSID sid = permission_domain ? AppContainerSid(std::wstring(
                                      permission_domain->begin(), permission_domain->end()))
                                : nullptr;
  if (sid == nullptr) return 70;
  const auto sid_text = SidString(sid);
  const auto helper_digest = Sha256File(ModulePath());
  const auto read_digest = RelativeRootsDigest(request->read_roots, request->slot_root);
  bool ok = permission_domain.has_value() && sid_text.has_value() &&
            helper_digest.has_value() && read_digest.has_value();
  if (ok && operation == L"provision") {
    ok = ProvisionSecurity(*request, sid);
  } else if (ok && operation == L"repair") {
    ok = RepairSecurity(*request, sid);
  } else if (ok && operation == L"unprovision-slot") {
    ok = UnprovisionSlotSecurity(*request, sid);
  } else if (ok && operation == L"unprovision-domain") {
    ok = UnprovisionDomainSecurity(*request, sid);
  } else if (ok && operation != L"attest") {
    ok = false;
  }
  std::string root_security;
  std::string tree_security;
  std::string security_failure;
  if (ok && operation != L"unprovision-slot" && operation != L"unprovision-domain") {
    ok = AttestSecurity(*request, sid, false, &root_security, &security_failure);
    if (ok && request->mode == L"full" && operation == L"attest") {
      ok = AttestSecurity(
          *request, sid, true, &tree_security, &security_failure, true);
    } else if (ok && (request->mode == L"strict" || operation == L"repair")) {
      ok = AttestSecurity(
          *request, sid, true, &tree_security, &security_failure, true);
    } else if (ok) {
      tree_security = root_security;
    }
  }
  FreeSid(sid);
  if (!ok) {
    std::cerr << "ecorex_sandbox_security:"
              << (security_failure.empty() ? "operation" : security_failure);
    return 70;
  }
  if (operation == L"unprovision-domain") {
    const auto profile = AppContainerName(
        std::wstring(permission_domain->begin(), permission_domain->end()));
    if (!profile) return 70;
    const HRESULT deleted = DeleteAppContainerProfile(profile->c_str());
    if (FAILED(deleted) && deleted != HRESULT_FROM_WIN32(ERROR_NOT_FOUND)) return 70;
    std::cout << "{\"operation\":\"unprovision-domain\",\"schema_version\":1,\"status\":\"passed\"}";
    return 0;
  }
  if (operation == L"unprovision-slot") {
    std::cout << "{\"operation\":\"unprovision-slot\",\"schema_version\":1,\"status\":\"passed\"}";
    return 0;
  }
  const char* inheritance_proof =
      (operation == L"repair" || request->mode == L"strict" ||
       (operation == L"attest" && request->mode == L"full"))
          ? "immutable-read-tree-mutable-workspace-acl-v2"
          : (operation == L"provision" ? "fresh-empty-roots-v1"
                                         : "root-identity-no-reparse-tree-v1");
  std::cout << "{\"appcontainer_sid\":\"" << Utf8(*sid_text)
            << "\",\"cpu_rate_hard_cap\":" << kCpuRate
            << ",\"helper_sha256\":\"" << *helper_digest
            << "\",\"inheritance_proof\":\"" << inheritance_proof
            << "\",\"job_memory_limit_bytes\":" << kJobMemoryLimit
            << ",\"operation\":\"" << Utf8(operation)
            << "\",\"permission_domain_sha256\":\"" << *permission_domain
            << "\",\"process_memory_limit_bytes\":" << kProcessMemoryLimit
            << ",\"read_roots_sha256\":\"" << *read_digest
            << "\",\"root_security_sha256\":\"" << root_security
            << "\",\"schema_version\":1,\"slot_digest\":\""
            << Utf8(request->slot_digest)
            << "\",\"status\":\"passed\",\"tree_security_sha256\":\""
            << tree_security << "\",\"workspace_roots_sha256\":\""
            << Utf8(request->workspace_digest) << "\"}";
  return 0;
}

}  // namespace ecorex::sandbox
