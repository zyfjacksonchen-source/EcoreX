// Minimal product launcher for the relocatable Python closure.
// Built with /Brepro; it never evaluates a command string or consults PATH.

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>

#include <filesystem>
#include <string>
#include <vector>

namespace {

std::wstring Quote(const std::wstring& value) {
  std::wstring out = L"\"";
  unsigned backslashes = 0;
  for (const wchar_t ch : value) {
    if (ch == L'\\') {
      ++backslashes;
    } else if (ch == L'\"') {
      out.append(backslashes * 2 + 1, L'\\');
      out.push_back(ch);
      backslashes = 0;
    } else {
      out.append(backslashes, L'\\');
      backslashes = 0;
      out.push_back(ch);
    }
  }
  out.append(backslashes * 2, L'\\');
  out.push_back(L'\"');
  return out;
}

std::filesystem::path ModulePath() {
  std::vector<wchar_t> buffer(32768);
  const DWORD size = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
  if (size == 0 || size >= static_cast<DWORD>(buffer.size())) return {};
  return std::filesystem::path(std::wstring(buffer.data(), size));
}

}  // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
  const auto launcher = ModulePath();
  if (launcher.empty()) return 70;
  const auto python = launcher.parent_path() / L"pack-python" / L"python.exe";
  if (!std::filesystem::is_regular_file(python)) return 78;

  int argc = 0;
  LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
  if (argv == nullptr || argc < 1) return 70;
  std::wstring command = Quote(python.wstring()) + L" -I -B -m ecorex.server";
  for (int index = 1; index < argc; ++index) {
    command.push_back(L' ');
    command += Quote(argv[index]);
  }
  LocalFree(argv);

  HANDLE job = CreateJobObjectW(nullptr, nullptr);
  if (job == nullptr) return 70;
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
  limits.BasicLimitInformation.LimitFlags =
      JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION;
  if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
    CloseHandle(job);
    return 70;
  }

  STARTUPINFOW startup{};
  startup.cb = sizeof(startup);
  PROCESS_INFORMATION process{};
  std::vector<wchar_t> mutable_command(command.begin(), command.end());
  mutable_command.push_back(L'\0');
  const BOOL created = CreateProcessW(
      python.c_str(), mutable_command.data(), nullptr, nullptr, FALSE,
      CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
      nullptr, launcher.parent_path().parent_path().c_str(), &startup, &process);
  if (!created || !AssignProcessToJobObject(job, process.hProcess)) {
    if (created) {
      TerminateProcess(process.hProcess, 70);
      CloseHandle(process.hThread);
      CloseHandle(process.hProcess);
    }
    CloseHandle(job);
    return 70;
  }
  if (ResumeThread(process.hThread) == static_cast<DWORD>(-1)) {
    TerminateJobObject(job, 70);
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    CloseHandle(job);
    return 70;
  }
  CloseHandle(process.hThread);
  if (WaitForSingleObject(process.hProcess, INFINITE) != WAIT_OBJECT_0) {
    TerminateJobObject(job, 70);
    CloseHandle(process.hProcess);
    CloseHandle(job);
    return 70;
  }
  DWORD exit_code = 70;
  if (!GetExitCodeProcess(process.hProcess, &exit_code)) exit_code = 70;
  CloseHandle(process.hProcess);
  CloseHandle(job);
  return static_cast<int>(exit_code);
}
