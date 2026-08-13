const assert = require("node:assert/strict");
const Module = require("node:module");
const test = require("node:test");

const originalLoad = Module._load;
let executable = "/Applications/e-Mate.app/Contents/MacOS/e-Mate";
Module._load = function mockedLoad(request, parent, isMain) {
  if (request === "electron") return {
    app: {
      getPath: (name) => name === "exe" ? executable : "/tmp/emate-test",
      getVersion: () => "2.0.4",
    },
    dialog: {},
    net: {},
  };
  if (request === "electron-updater") return { autoUpdater: {} };
  return originalLoad.call(this, request, parent, isMain);
};
const updater = require("./updater.cjs");
Module._load = originalLoad;

test("macOS updater targets the installed app and quotes every filesystem path", () => {
  assert.equal(updater.installedMacAppPath(), "/Applications/e-Mate.app");
  executable = "/Volumes/e-Mate/e-Mate.app/Contents/MacOS/e-Mate";
  assert.equal(updater.installedMacAppPath(), "/Applications/e-Mate.app");
  const command = updater.macInstallCommand(
    "/Users/Test O'Neil/.emate/staged/e-Mate.app",
    "/Applications/e-Mate.app",
  );
  assert.match(command, /ditto --rsrc --extattr --acl/u);
  assert.match(command, /'"'"'/u);
  assert.match(command, /\.emate-update-backup/u);
  assert.match(command, /else.*backup.*exit 1/u);
});
