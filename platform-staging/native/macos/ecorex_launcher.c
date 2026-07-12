/* Relocatable macOS launcher. It replaces itself with the fixed Pack Python. */

#include <mach-o/dyld.h>
#include <libgen.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
  char unresolved[PATH_MAX];
  char module[PATH_MAX];
  uint32_t size = sizeof(unresolved);
  if (_NSGetExecutablePath(unresolved, &size) != 0 ||
      realpath(unresolved, module) == NULL) {
    return 70;
  }
  char directory_buffer[PATH_MAX];
  const int directory_length =
      snprintf(directory_buffer, sizeof(directory_buffer), "%s", module);
  if (directory_length < 0 || (size_t)directory_length >= sizeof(directory_buffer)) {
    return 70;
  }
  const char *directory = dirname(directory_buffer);
  char python[PATH_MAX];
  const int python_length =
      snprintf(python, sizeof(python), "%s/pack-python/bin/python3", directory);
  if (python_length < 0 || (size_t)python_length >= sizeof(python)) return 70;
  if (access(python, X_OK) != 0) return 78;

  char **child = calloc((size_t)argc + 6, sizeof(char *));
  if (child == NULL) return 70;
  child[0] = python;
  child[1] = "-I";
  child[2] = "-B";
  child[3] = "-m";
  child[4] = "ecorex.server";
  for (int index = 1; index < argc; ++index) child[index + 4] = argv[index];
  child[argc + 4] = NULL;
  execv(python, child);
  return 70;
}
