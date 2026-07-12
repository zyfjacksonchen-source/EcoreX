// AppContainer process launch, Job Object containment, and bounded pipe relay.
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>

#include "ecorex_sandbox_host_internal.h"

#include <sddl.h>
#include <userenv.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <fstream>
#include <iostream>
#include <sstream>
#include <thread>

namespace ecorex::sandbox {

struct ChildResult {
  DWORD exit_code = 70;
  bool timed_out = false;
  bool process_tree_contained = false;
  std::string captured;
  const char* stage = "not_started";
  DWORD win32_error = ERROR_SUCCESS;
};

void CloseHandles(std::vector<HANDLE>& handles) {
  for (const HANDLE handle : handles) {
    if (handle != nullptr && handle != INVALID_HANDLE_VALUE) CloseHandle(handle);
  }
  handles.clear();
}

bool InitializePipeSecurity(PSID sid, LocalPointer* owned_descriptor,
                            SECURITY_ATTRIBUTES* attributes) {
  if (sid == nullptr || owned_descriptor == nullptr || attributes == nullptr) {
    return false;
  }
  HANDLE token = nullptr;
  DWORD user_bytes = 0;
  if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return false;
  GetTokenInformation(token, TokenUser, nullptr, 0, &user_bytes);
  std::vector<unsigned char> user_buffer(user_bytes);
  if (user_bytes == 0 ||
      !GetTokenInformation(token, TokenUser, user_buffer.data(), user_bytes, &user_bytes)) {
    CloseHandle(token);
    return false;
  }
  CloseHandle(token);
  const auto* token_user = reinterpret_cast<const TOKEN_USER*>(user_buffer.data());
  LPWSTR user_text = nullptr;
  LPWSTR appcontainer_text = nullptr;
  if (!ConvertSidToStringSidW(token_user->User.Sid, &user_text) ||
      !ConvertSidToStringSidW(sid, &appcontainer_text)) {
    if (user_text) LocalFree(user_text);
    if (appcontainer_text) LocalFree(appcontainer_text);
    return false;
  }
  LocalPointer owned_user_text(user_text);
  LocalPointer owned_appcontainer_text(appcontainer_text);
  const std::wstring sddl = L"D:(A;;GA;;;" + std::wstring(user_text) + L")(A;;GA;;;" +
                            std::wstring(appcontainer_text) + L")S:(ML;;NW;;;LW)";
  PSECURITY_DESCRIPTOR descriptor = nullptr;
  if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
          sddl.c_str(), SDDL_REVISION_1, &descriptor, nullptr)) {
    return false;
  }
  owned_descriptor->reset(descriptor);
  *attributes = SECURITY_ATTRIBUTES{sizeof(SECURITY_ATTRIBUTES), descriptor, TRUE};
  return true;
}

bool CreateRelayPipe(HANDLE* parent, HANDLE* child, bool child_reads,
                     SECURITY_ATTRIBUTES* attributes) {
  HANDLE read_handle = nullptr;
  HANDLE write_handle = nullptr;
  if (!CreatePipe(&read_handle, &write_handle, attributes, 0)) return false;
  *child = child_reads ? read_handle : write_handle;
  *parent = child_reads ? write_handle : read_handle;
  if (!SetHandleInformation(*parent, HANDLE_FLAG_INHERIT, 0)) {
    CloseHandle(read_handle);
    CloseHandle(write_handle);
    *parent = nullptr;
    *child = nullptr;
    return false;
  }
  return true;
}

bool WriteAll(HANDLE handle, const char* data, size_t size) {
  size_t offset = 0;
  while (offset < size) {
    const DWORD chunk = static_cast<DWORD>(std::min<size_t>(size - offset, 64 * 1024));
    DWORD written = 0;
    if (!WriteFile(handle, data + offset, chunk, &written, nullptr) || written == 0) return false;
    offset += written;
  }
  return true;
}

std::optional<std::string> ReadInput(DWORD limit) {
  const HANDLE input = GetStdHandle(STD_INPUT_HANDLE);
  if (input == nullptr || input == INVALID_HANDLE_VALUE) return std::nullopt;
  std::string payload;
  std::array<char, 16 * 1024> buffer{};
  while (true) {
    DWORD read = 0;
    if (!ReadFile(input, buffer.data(), static_cast<DWORD>(buffer.size()), &read, nullptr)) {
      if (GetLastError() == ERROR_BROKEN_PIPE) break;
      return std::nullopt;
    }
    if (read == 0) break;
    if (payload.size() + read > limit) return std::nullopt;
    payload.append(buffer.data(), read);
  }
  return payload;
}

void RelayOutput(HANDLE source, HANDLE destination, DWORD limit, bool capture,
                 std::string* captured, std::atomic<bool>* limit_exceeded,
                 std::atomic<bool>* transport_failed) {
  std::array<char, 16 * 1024> buffer{};
  DWORD total = 0;
  while (true) {
    DWORD read = 0;
    if (!ReadFile(source, buffer.data(), static_cast<DWORD>(buffer.size()), &read, nullptr)) {
      if (GetLastError() != ERROR_BROKEN_PIPE) transport_failed->store(true);
      break;
    }
    if (read == 0) break;
    if (total > limit || read > limit - total) {
      limit_exceeded->store(true);
      break;
    }
    total += read;
    if (capture) {
      captured->append(buffer.data(), read);
    } else if (!WriteAll(destination, buffer.data(), read)) {
      transport_failed->store(true);
      break;
    }
  }
  CloseHandle(source);
}

ChildResult Launch(const std::filesystem::path& executable,
                   const std::vector<std::wstring>& arguments,
                   const std::filesystem::path& cwd, DWORD timeout_ms,
                   PSID appcontainer_sid, bool capture, const std::string& input,
                   DWORD output_limit) {
  ChildResult result;
  HANDLE job = CreateJobObjectW(nullptr, nullptr);
  if (job == nullptr) {
    result.stage = "create_job";
    result.win32_error = GetLastError();
    return result;
  }
  JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
  limits.BasicLimitInformation.LimitFlags =
      JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION |
      JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_PROCESS_MEMORY |
      JOB_OBJECT_LIMIT_JOB_MEMORY;
  limits.BasicLimitInformation.ActiveProcessLimit = 64;
  limits.ProcessMemoryLimit = kProcessMemoryLimit;
  limits.JobMemoryLimit = kJobMemoryLimit;
  if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
    result.stage = "job_policy";
    result.win32_error = GetLastError();
    CloseHandle(job);
    return result;
  }
  JOBOBJECT_CPU_RATE_CONTROL_INFORMATION cpu{};
  cpu.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE |
                     JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP;
  cpu.CpuRate = kCpuRate;
  if (!SetInformationJobObject(job, JobObjectCpuRateControlInformation, &cpu,
                               sizeof(cpu))) {
    result.stage = "job_cpu_policy";
    result.win32_error = GetLastError();
    CloseHandle(job);
    return result;
  }

  HANDLE parent_stdin = nullptr;
  HANDLE parent_stdout = nullptr;
  HANDLE parent_stderr = nullptr;
  std::vector<HANDLE> child_handles;
  HANDLE child_stdin = nullptr;
  HANDLE child_stdout = nullptr;
  HANDLE child_stderr = nullptr;
  LocalPointer pipe_descriptor;
  SECURITY_ATTRIBUTES inheritable{sizeof(inheritable), nullptr, TRUE};
  if (appcontainer_sid != nullptr &&
      !InitializePipeSecurity(appcontainer_sid, &pipe_descriptor, &inheritable)) {
    result.stage = "pipe_security";
    result.win32_error = GetLastError();
    CloseHandle(job);
    return result;
  }
  if (!CreateRelayPipe(&parent_stdin, &child_stdin, true, &inheritable) ||
      !CreateRelayPipe(&parent_stdout, &child_stdout, false, &inheritable) ||
      !CreateRelayPipe(&parent_stderr, &child_stderr, false, &inheritable)) {
    result.stage = "relay_pipe";
    result.win32_error = GetLastError();
    std::vector<HANDLE> relay_handles = {
        parent_stdin, parent_stdout, parent_stderr, child_stdin, child_stdout, child_stderr};
    CloseHandles(relay_handles);
    CloseHandle(job);
    return result;
  }
  child_handles = {child_stdin, child_stdout, child_stderr};

  SIZE_T attribute_bytes = 0;
  STARTUPINFOEXW startup{};
  startup.StartupInfo.cb = sizeof(startup);
  startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
  startup.StartupInfo.hStdInput = child_stdin;
  startup.StartupInfo.hStdOutput = child_stdout;
  startup.StartupInfo.hStdError = child_stderr;
  SECURITY_CAPABILITIES capabilities{};
  const DWORD attribute_count = appcontainer_sid != nullptr ? 2 : 1;
  InitializeProcThreadAttributeList(nullptr, attribute_count, 0, &attribute_bytes);
  startup.lpAttributeList = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(
      HeapAlloc(GetProcessHeap(), 0, attribute_bytes));
  if (startup.lpAttributeList == nullptr ||
      !InitializeProcThreadAttributeList(startup.lpAttributeList, attribute_count, 0,
                                         &attribute_bytes)) {
    result.stage = "attribute_list";
    result.win32_error = GetLastError();
    if (startup.lpAttributeList) HeapFree(GetProcessHeap(), 0, startup.lpAttributeList);
    CloseHandle(parent_stdin);
    CloseHandle(parent_stdout);
    CloseHandle(parent_stderr);
    CloseHandles(child_handles);
    CloseHandle(job);
    return result;
  }
  if (!UpdateProcThreadAttribute(startup.lpAttributeList, 0,
                                 PROC_THREAD_ATTRIBUTE_HANDLE_LIST, child_handles.data(),
                                 sizeof(HANDLE) * child_handles.size(), nullptr, nullptr)) {
    result.stage = "handle_list";
    result.win32_error = GetLastError();
    DeleteProcThreadAttributeList(startup.lpAttributeList);
    HeapFree(GetProcessHeap(), 0, startup.lpAttributeList);
    CloseHandle(parent_stdin);
    CloseHandle(parent_stdout);
    CloseHandle(parent_stderr);
    CloseHandles(child_handles);
    CloseHandle(job);
    return result;
  }
  if (appcontainer_sid != nullptr) {
    capabilities.AppContainerSid = appcontainer_sid;
    if (!UpdateProcThreadAttribute(startup.lpAttributeList, 0,
                                   PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
                                   &capabilities, sizeof(capabilities), nullptr, nullptr)) {
      result.stage = "security_capabilities";
      result.win32_error = GetLastError();
      DeleteProcThreadAttributeList(startup.lpAttributeList);
      HeapFree(GetProcessHeap(), 0, startup.lpAttributeList);
      CloseHandle(parent_stdin);
      CloseHandle(parent_stdout);
      CloseHandle(parent_stderr);
      CloseHandles(child_handles);
      CloseHandle(job);
      return result;
    }
  }

  std::wstring command = Quote(executable.wstring());
  for (const auto& argument : arguments) command += L" " + Quote(argument);
  std::vector<wchar_t> mutable_command(command.begin(), command.end());
  mutable_command.push_back(L'\0');
  PROCESS_INFORMATION process{};
  const DWORD flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW |
                      EXTENDED_STARTUPINFO_PRESENT;
  HANDLE process_token = nullptr;
  HANDLE restricted_token = nullptr;
  if (appcontainer_sid != nullptr &&
      (!OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY |
                                                  TOKEN_QUERY,
                         &process_token) ||
       !CreateRestrictedToken(process_token, DISABLE_MAX_PRIVILEGE, 0, nullptr, 0, nullptr,
                              0, nullptr, &restricted_token))) {
    result.stage = "restricted_token";
    result.win32_error = GetLastError();
    if (process_token) CloseHandle(process_token);
    if (startup.lpAttributeList != nullptr) {
      DeleteProcThreadAttributeList(startup.lpAttributeList);
      HeapFree(GetProcessHeap(), 0, startup.lpAttributeList);
    }
    CloseHandle(parent_stdin);
    CloseHandle(parent_stdout);
    CloseHandle(parent_stderr);
    CloseHandles(child_handles);
    CloseHandle(job);
    return result;
  }
  if (process_token) CloseHandle(process_token);
  const BOOL created = appcontainer_sid != nullptr
                           ? CreateProcessAsUserW(
                                 restricted_token, executable.c_str(), mutable_command.data(),
                                 nullptr, nullptr, TRUE, flags, nullptr, cwd.c_str(),
                                 &startup.StartupInfo, &process)
                           : CreateProcessW(
                                 executable.c_str(), mutable_command.data(), nullptr, nullptr,
                                 TRUE, flags, nullptr, cwd.c_str(), &startup.StartupInfo, &process);
  const DWORD creation_error = created ? ERROR_SUCCESS : GetLastError();
  if (restricted_token) CloseHandle(restricted_token);
  if (startup.lpAttributeList != nullptr) {
    DeleteProcThreadAttributeList(startup.lpAttributeList);
    HeapFree(GetProcessHeap(), 0, startup.lpAttributeList);
  }
  CloseHandles(child_handles);
  if (!created) {
    result.stage = "create_process";
    result.win32_error = creation_error;
    CloseHandle(parent_stdin);
    CloseHandle(parent_stdout);
    CloseHandle(parent_stderr);
    CloseHandle(job);
    return result;
  }
  if (!AssignProcessToJobObject(job, process.hProcess)) {
    result.stage = "assign_job";
    result.win32_error = GetLastError();
    if (created) {
      TerminateProcess(process.hProcess, 70);
      CloseHandle(process.hThread);
      CloseHandle(process.hProcess);
    }
    CloseHandle(parent_stdin);
    CloseHandle(parent_stdout);
    CloseHandle(parent_stderr);
    CloseHandle(job);
    return result;
  }
  std::atomic<bool> output_limit_exceeded{false};
  std::atomic<bool> transport_failed{false};
  std::string stderr_capture;
  std::thread stdout_relay(
      RelayOutput, parent_stdout, GetStdHandle(STD_OUTPUT_HANDLE), output_limit, capture,
      &result.captured, &output_limit_exceeded, &transport_failed);
  std::thread stderr_relay(
      RelayOutput, parent_stderr, GetStdHandle(STD_ERROR_HANDLE), 64 * 1024, capture,
      &stderr_capture, &output_limit_exceeded, &transport_failed);
  const DWORD resume_result = ResumeThread(process.hThread);
  const DWORD resume_error = resume_result == static_cast<DWORD>(-1) ? GetLastError() : ERROR_SUCCESS;
  CloseHandle(process.hThread);
  if (resume_result == static_cast<DWORD>(-1)) {
    result.stage = "resume_thread";
    result.win32_error = resume_error;
    CloseHandle(parent_stdin);
    TerminateJobObject(job, 70);
    WaitForSingleObject(process.hProcess, 5000);
    stdout_relay.join();
    stderr_relay.join();
    CloseHandle(process.hProcess);
    CloseHandle(job);
    return result;
  }
  std::thread stdin_relay([parent_stdin, &input, &transport_failed]() {
    if (!input.empty() && !WriteAll(parent_stdin, input.data(), input.size())) {
      transport_failed.store(true);
    }
    CloseHandle(parent_stdin);
  });
  const ULONGLONG wait_started = GetTickCount64();
  DWORD wait = WAIT_TIMEOUT;
  while ((wait = WaitForSingleObject(process.hProcess, 50)) == WAIT_TIMEOUT &&
         !output_limit_exceeded.load() && !transport_failed.load() &&
         GetTickCount64() - wait_started < timeout_ms) {
  }
  if (wait == WAIT_FAILED) {
    result.stage = "wait_process";
    result.win32_error = GetLastError();
    TerminateJobObject(job, 70);
    WaitForSingleObject(process.hProcess, 5000);
  } else if (wait == WAIT_TIMEOUT &&
             !output_limit_exceeded.load() && !transport_failed.load()) {
    result.timed_out = true;
    TerminateJobObject(job, 1460);
    WaitForSingleObject(process.hProcess, 5000);
  } else if (output_limit_exceeded.load() || transport_failed.load()) {
    TerminateJobObject(job, 70);
    WaitForSingleObject(process.hProcess, 5000);
  }
  GetExitCodeProcess(process.hProcess, &result.exit_code);
  JOBOBJECT_BASIC_ACCOUNTING_INFORMATION accounting{};
  if (QueryInformationJobObject(job, JobObjectBasicAccountingInformation,
                                &accounting, sizeof(accounting), nullptr)) {
    result.process_tree_contained = accounting.TotalProcesses >= 1;
  }
  stdin_relay.join();
  stdout_relay.join();
  stderr_relay.join();
  CloseHandle(process.hProcess);
  CloseHandle(job);
  if (wait != WAIT_FAILED) {
    result.stage = output_limit_exceeded.load()
                       ? "output_limit"
                       : (transport_failed.load()
                              ? "stdio_relay"
                              : (result.timed_out ? "timeout" : "completed"));
  }
  return result;
}

int ProbeChild(const std::filesystem::path& workspace,
               const std::filesystem::path& outside, unsigned short port) {
  bool inside_write = false;
  bool outside_read = false;
  bool outside_write = false;
  bool network = false;
  {
    std::ofstream output(workspace / L"inside.txt", std::ios::binary);
    output << "ok";
    inside_write = output.good();
  }
  {
    std::ifstream input(outside, std::ios::binary);
    char value = 0;
    outside_read = static_cast<bool>(input.get(value));
  }
  {
    std::ofstream output(outside, std::ios::binary | std::ios::trunc);
    output << "escape";
    outside_write = output.good();
  }
  WSADATA data{};
  if (WSAStartup(MAKEWORD(2, 2), &data) == 0) {
    SOCKET client = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (client != INVALID_SOCKET) {
      u_long nonblocking = 1;
      ioctlsocket(client, FIONBIO, &nonblocking);
      sockaddr_in endpoint{};
      endpoint.sin_family = AF_INET;
      endpoint.sin_port = htons(port);
      InetPtonW(AF_INET, L"127.0.0.1", &endpoint.sin_addr);
      const int connected =
          connect(client, reinterpret_cast<sockaddr*>(&endpoint), sizeof(endpoint));
      if (connected == 0) {
        network = true;
      } else if (WSAGetLastError() == WSAEWOULDBLOCK) {
        fd_set writable;
        fd_set failed;
        FD_ZERO(&writable);
        FD_ZERO(&failed);
        FD_SET(client, &writable);
        FD_SET(client, &failed);
        timeval wait{0, 200000};
        if (select(0, nullptr, &writable, &failed, &wait) > 0 && FD_ISSET(client, &writable)) {
          int socket_error = 0;
          int socket_error_size = sizeof(socket_error);
          network = getsockopt(client, SOL_SOCKET, SO_ERROR,
                               reinterpret_cast<char*>(&socket_error),
                               &socket_error_size) == 0 &&
                    socket_error == 0;
        }
      }
      closesocket(client);
    }
    WSACleanup();
  }
  std::ostringstream response;
  response << "{\"inside_write\":" << (inside_write ? "true" : "false")
           << ",\"network\":" << (network ? "true" : "false")
           << ",\"outside_read\":" << (outside_read ? "true" : "false")
           << ",\"outside_write\":" << (outside_write ? "true" : "false") << "}";
  const auto payload = response.str();
  DWORD written = 0;
  const BOOL relayed = WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), payload.data(),
                                 static_cast<DWORD>(payload.size()), &written, nullptr);
  return (inside_write ? 1 : 0) | (outside_read ? 2 : 0) | (outside_write ? 4 : 0) |
         (network ? 8 : 0) | (relayed && written == payload.size() ? 16 : 0);
}

int Probe(const std::wstring& expected_digest,
          const std::vector<std::filesystem::path>& workspaces) {
  const auto module = ModulePath();
  const auto roots_digest = RootsDigest(workspaces);
  if (module.empty() || workspaces.empty() || !roots_digest ||
      Utf8(expected_digest) != *roots_digest) {
    std::cerr << "ecorex_sandbox_probe:module_path";
    return 70;
  }
  const auto base = workspaces.front() /
                    (L"ecorex-appcontainer-probe-" + std::to_wstring(GetCurrentProcessId()));
  const auto workspace = base / L"workspace";
  const auto outside = workspaces.front().parent_path() /
                       (L"ecorex-appcontainer-outside-" +
                        std::to_wstring(GetCurrentProcessId()) + L".txt");
  std::error_code error;
  std::filesystem::create_directories(workspace, error);
  if (error) {
    std::cerr << "ecorex_sandbox_probe:workspace_create";
    return 70;
  }
  {
    std::ofstream output(outside, std::ios::binary);
    output << "outside";
  }
  SOCKET listener = INVALID_SOCKET;
  unsigned short port = 0;
  WSADATA data{};
  if (WSAStartup(MAKEWORD(2, 2), &data) == 0) {
    listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = 0;
    if (listener != INVALID_SOCKET &&
        bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0 &&
        listen(listener, 1) == 0) {
      int length = sizeof(address);
      getsockname(listener, reinterpret_cast<sockaddr*>(&address), &length);
      port = ntohs(address.sin_port);
    }
  }
  const auto permission_domain = PermissionDomainDigest(workspaces);
  PSID sid = permission_domain
                 ? AppContainerSid(std::wstring(permission_domain->begin(),
                                                permission_domain->end()))
                 : nullptr;
  if (sid == nullptr) {
    std::cerr << "ecorex_sandbox_probe:appcontainer_sid";
    return 70;
  }
  if (port == 0) {
    std::cerr << "ecorex_sandbox_probe:loopback_listener";
    FreeSid(sid);
    return 70;
  }
  if (!PrepareProbeWorkspace(workspace, sid)) {
    std::cerr << "ecorex_sandbox_probe:workspace_security";
    FreeSid(sid);
    closesocket(listener);
    WSACleanup();
    std::filesystem::remove_all(base, error);
    std::filesystem::remove(outside, error);
    return 70;
  }
  const auto child = Launch(
      module,
      {L"probe-child", workspace.wstring(), outside.wstring(), std::to_wstring(port)},
      workspace, 10000, sid, true, {}, 64 * 1024);
  FreeSid(sid);
  if (listener != INVALID_SOCKET) closesocket(listener);
  WSACleanup();
  std::filesystem::remove_all(base, error);
  std::filesystem::remove(outside, error);
  const std::string expected =
      "{\"inside_write\":true,\"network\":false,\"outside_read\":false,\"outside_write\":false}";
  if (child.exit_code != 17 || child.timed_out || !child.process_tree_contained ||
      child.captured != expected) {
    std::cerr << "ecorex_sandbox_probe:child_boundary:" << child.stage
              << ":" << child.win32_error << ":" << child.exit_code;
    return 70;
  }
  const auto digest = Utf8(expected_digest);
  std::cout
      << "{\"backend\":\"windows-appcontainer\",\"cpu_rate_hard_cap\":" << kCpuRate
      << ",\"filesystem_read_scoped\":true,"
      << "\"filesystem_write_scoped\":true,\"job_memory_limit_bytes\":"
      << kJobMemoryLimit << ",\"network_denied\":true"
      << ",\"process_memory_limit_bytes\":" << kProcessMemoryLimit
      << ",\"process_tree_contained\":true,\"protocol\":\"ecorex-sandbox-launch-v1\","
      << "\"workspace_roots_sha256\":\"" << digest << "\"}";
  return 0;
}

int Run(int argc, wchar_t** argv) {
  std::wstring profile;
  std::wstring network;
  std::wstring expected_roots;
  std::wstring expected_artifact;
  std::wstring slot_digest;
  std::wstring security_digest;
  std::filesystem::path install_root;
  std::filesystem::path slot_root;
  std::vector<std::filesystem::path> read_roots;
  DWORD timeout_ms = 0;
  DWORD output_limit = 0;
  unsigned long long process_memory_limit = 0;
  unsigned long long job_memory_limit = 0;
  DWORD cpu_rate = 0;
  std::vector<std::filesystem::path> workspaces;
  int child_index = -1;
  for (int index = 2; index < argc; ++index) {
    const std::wstring key = argv[index];
    if (key == L"--" && child_index < 0) {
      child_index = index + 1;
      break;
    }
    if (index + 1 >= argc) return 64;
    const std::wstring value = argv[++index];
    if (key == L"--protocol") {
      if (value != kProtocol) return 64;
    } else if (key == L"--profile") {
      profile = value;
    } else if (key == L"--network") {
      network = value;
    } else if (key == L"--timeout-ms") {
      timeout_ms = std::stoul(value);
    } else if (key == L"--output-limit") {
      output_limit = std::stoul(value);
    } else if (key == L"--process-memory-limit") {
      process_memory_limit = std::stoull(value);
    } else if (key == L"--job-memory-limit") {
      job_memory_limit = std::stoull(value);
    } else if (key == L"--cpu-rate") {
      cpu_rate = std::stoul(value);
    } else if (key == L"--workspace-digest") {
      expected_roots = value;
    } else if (key == L"--artifact-sha256") {
      expected_artifact = value;
    } else if (key == L"--slot-digest") {
      slot_digest = value;
    } else if (key == L"--security-digest") {
      security_digest = value;
    } else if (key == L"--install-root") {
      const auto root = AbsoluteDirectory(value);
      if (!root || !install_root.empty()) return 64;
      install_root = *root;
    } else if (key == L"--slot-root") {
      const auto root = AbsoluteDirectory(value);
      if (!root || !slot_root.empty()) return 64;
      slot_root = *root;
    } else if (key == L"--read-root") {
      const auto root = AbsoluteDirectory(value);
      if (!root) return 64;
      read_roots.push_back(*root);
    } else if (key == L"--workspace") {
      const auto root = AbsoluteDirectory(value);
      if (!root) return 64;
      workspaces.push_back(*root);
    } else {
      return 64;
    }
  }
  if (child_index < 0 || argc - child_index != 3 || workspaces.empty() ||
      timeout_ms < 1 ||
      output_limit < 1 || output_limit > 4 * 1024 * 1024 ||
      process_memory_limit != kProcessMemoryLimit ||
      job_memory_limit != kJobMemoryLimit || cpu_rate != kCpuRate ||
      expected_roots.size() != 64 || expected_artifact.size() != 64 ||
      !IsSha256(slot_digest) || !IsSha256(security_digest) ||
      install_root.empty() || slot_root.empty() || read_roots.empty() ||
      (profile != L"workspace-write" && profile != L"danger-full-access") ||
      (profile == L"workspace-write" && (network != L"deny" || workspaces.empty())) ||
      (profile == L"danger-full-access" && network != L"allow")) return 64;
  const auto python = AbsoluteRegular(argv[child_index]);
  const auto artifact = AbsoluteRegular(argv[child_index + 2]);
  if (!python || !artifact || std::wstring(argv[child_index + 1]) != L"-I") return 64;
  const SecurityRoots security{
      install_root, slot_root, slot_digest, expected_roots, read_roots, workspaces, L"roots"};
  const auto runtime_helper = AbsoluteRegular(ModulePath().wstring());
  if (!ValidateSecurityRoots(security) || !runtime_helper ||
      !ContainedPath(*runtime_helper, slot_root) ||
      !std::any_of(read_roots.begin(), read_roots.end(),
                   [&python](const auto& root) { return ContainedPath(*python, root); }) ||
      !std::any_of(read_roots.begin(), read_roots.end(),
                   [&artifact](const auto& root) { return ContainedPath(*artifact, root); })) {
    return 78;
  }
  const auto roots_digest = RootsDigest(workspaces);
  const auto artifact_digest = Sha256File(*artifact);
  if (!roots_digest || !artifact_digest || Utf8(expected_roots) != *roots_digest ||
      Utf8(expected_artifact) != *artifact_digest) return 78;
  const auto input = ReadInput(512 * 1024);
  if (!input) return 65;

  const auto permission_domain = PermissionDomainDigest(workspaces);
  PSID sid = permission_domain
                 ? AppContainerSid(std::wstring(permission_domain->begin(),
                                                permission_domain->end()))
                 : nullptr;
  if (sid == nullptr) return 70;
  std::string observed_security;
  if (!AttestSecurity(security, sid, false, &observed_security) ||
      observed_security != Utf8(security_digest)) {
    FreeSid(sid);
    return 78;
  }
  if (profile == L"workspace-write") {
    // The install transaction permanently provisions these product-owned
    // roots.  The hot path only attests their root descriptors; it never
    // mutates ACLs or integrity labels and therefore remains concurrent.
  } else {
    FreeSid(sid);
    sid = nullptr;
  }
  const auto result = Launch(*python, {L"-I", artifact->wstring()}, workspaces.front(),
                             timeout_ms, sid, false, *input, output_limit);
  if (sid) FreeSid(sid);
  if (result.timed_out) return 1460;
  if (!result.process_tree_contained) return 70;
  return static_cast<int>(result.exit_code);
}

}  // namespace ecorex::sandbox
