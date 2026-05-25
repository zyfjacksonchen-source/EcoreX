# kkFileView Packaging Integration

This note covers the packaging prep layer only. It does not change
`package.json`, `electron/main.cjs`, or renderer code.

## Local Findings

Initial inspection of `C:\kkFileView-master` on 2026-05-25 found:

- A Maven parent project at `pom.xml` with module `server`.
- `server/pom.xml` builds artifact `kkFileView` version `5.0.0`.
- Java is configured as version `21`.
- The server module uses `spring-boot-maven-plugin` repackage.
- The assembly plugin has Windows and Linux descriptors:
  `server/src/main/assembly/dist-win32.xml` and
  `server/src/main/assembly/dist-linux.xml`.
- No built `server/target/kkFileView-*.jar` was present yet.
- Windows LibreOffice Portable exists under
  `server/LibreOfficePortable` and includes `program/soffice.exe`.
- No local `java` or `mvn` command was available in this shell.

The detected Maven build command is:

```powershell
cd C:\kkFileView-master
mvn -pl server -am -DskipTests package
```

Expected build outputs are:

```text
server/target/kkFileView-5.0.0.jar
server/target/kkFileView-5.0.0.zip
server/target/kkFileView-5.0.0.tar.gz
```

## Scripts

Dry-run discovery without writing `vendor/kkfileview`:

```powershell
node scripts\prepare-kkfileview.cjs --dry-run
```

Build and prepare vendor files after Maven and Java 21 are installed:

```powershell
node scripts\prepare-kkfileview.cjs --build --copy --jre C:\path\to\java-21-runtime
```

If the jar has already been built, skip `--build`:

```powershell
node scripts\prepare-kkfileview.cjs --copy --jre C:\path\to\java-21-runtime
```

Verify the populated vendor directory:

```powershell
node scripts\verify-kkfileview-vendor.cjs --require-jre --require-libreoffice
```

Before vendor files exist, this is useful for CI or local smoke checks:

```powershell
node scripts\verify-kkfileview-vendor.cjs --allow-missing
```

## Vendor Layout

The prepare script generates this layout:

```text
vendor/kkfileview/
  manifest.json
  README.md
  bin/start-kkfileview.cmd
  bin/start-kkfileview.ps1
  bin/start-kkfileview.sh
  server/bin/kkFileView-*.jar
  server/config/application.properties
  runtime/log/
  jre/bin/java(.exe)
  libreoffice/LibreOfficePortable/App/libreoffice/program/soffice(.exe)
```

`manifest.json` records the source checkout, detected Maven command,
detected or copied jar, expected runtime paths, and the later
Electron Builder `extraResources` draft.

## Future extraResources Draft

Add this later when `vendor/kkfileview` has been verified:

```json
{
  "from": "vendor/kkfileview",
  "to": "kkfileview",
  "filter": [
    "**/*",
    "!**/*.log",
    "!**/tmp/**/*",
    "!**/.DS_Store",
    "!**/Thumbs.db"
  ]
}
```

The app runtime should treat `resourcesPath/kkfileview` as read-only
except for logs if a launcher script is used. Prefer putting kkFileView
working directories under Electron `app.getPath("userData")`, then pass
environment variables before starting the Java process:

```text
KK_SERVER_PORT=8012
KK_OFFICE_HOME=<resourcesPath>/kkfileview/libreoffice/LibreOfficePortable/App/libreoffice
KK_FILE_DIR=<userData>/kkfileview/files
KK_LOCAL_PREVIEW_DIR=<userData>/kkfileview/preview
KK_LOG_DIR=<userData>/kkfileview/log
KK_BASE_URL=http://127.0.0.1:8012
```

The generated launcher drafts set these values for direct local testing.
Electron can also spawn the bundled `jre/bin/java(.exe)` directly and
reuse the same manifest paths.
