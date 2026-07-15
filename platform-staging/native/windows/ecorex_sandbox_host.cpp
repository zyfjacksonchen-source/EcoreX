// EcoreX Windows sandbox helper protocol dispatcher.
#include "ecorex_sandbox_host_internal.h"

#include <string>

using namespace ecorex::sandbox;

int wmain(int argc, wchar_t** argv) {
  if (argc >= 2 && std::wstring(argv[1]) == L"probe-child") {
    if (argc != 5) return 64;
    return ProbeChild(argv[2], argv[3], static_cast<unsigned short>(std::stoul(argv[4])));
  }
  if (argc >= 2 &&
      (std::wstring(argv[1]) == L"provision" ||
       std::wstring(argv[1]) == L"repair" ||
       std::wstring(argv[1]) == L"attest" ||
       std::wstring(argv[1]) == L"unprovision-slot" ||
       std::wstring(argv[1]) == L"unprovision-domain")) {
    try {
      return SecurityCommand(argv[1], argc, argv);
    } catch (...) {
      return 64;
    }
  }
  if (argc >= 2 && std::wstring(argv[1]) == L"probe") {
    std::wstring expected_digest;
    std::vector<std::filesystem::path> workspaces;
    bool protocol = false;
    for (int index = 2; index < argc; ++index) {
      if (index + 1 >= argc) return 64;
      const std::wstring key = argv[index];
      const std::wstring value = argv[++index];
      if (key == L"--protocol") {
        if (protocol || value != kProtocol) return 64;
        protocol = true;
      } else if (key == L"--workspace-digest") {
        if (!expected_digest.empty() || !IsSha256(value)) return 64;
        expected_digest = value;
      } else if (key == L"--workspace") {
        const auto root = AbsoluteDirectory(value);
        if (!root) return 64;
        workspaces.push_back(*root);
      } else {
        return 64;
      }
    }
    if (!protocol || expected_digest.empty() || workspaces.empty()) return 64;
    return Probe(expected_digest, workspaces);
  }
  if (argc >= 2 && std::wstring(argv[1]) == L"run") {
    try {
      return Run(argc, argv);
    } catch (...) {
      return 64;
    }
  }
  return 64;
}
