"""Immutable provenance contract for the Python 3.11.9 macOS runtime closure."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping


@dataclass(frozen=True, slots=True)
class MacOSNativeComponent:
    name: str
    version: str
    license: str
    source_sha256: str
    notice_token: bytes
    license_text: str


@dataclass(frozen=True, slots=True)
class MacOSNativeLicenseText:
    archive_path: str
    repository_path: str
    size_bytes: int
    sha256: str
    source_url: str
    source_archive_sha256: str
    source_internal_path: str
    provenance: str


PYTHON_MACOS_DISTRIBUTION: Final[Mapping[str, object]] = MappingProxyType(
    {
        "url": ("https://www.python.org/ftp/python/3.11.9/python-3.11.9-macos11.pkg"),
        "size_bytes": 44_860_848,
        "sha256": "b6cfdee2571ca56ee895043ca1e7110fb78a878cee3eb0c21accb2de34d24b55",
    }
)

PYTHON_MACOS_LICENSE: Final[Mapping[str, object]] = MappingProxyType(
    {
        "path": "licenses/python-macos-installer-License.rtf",
        "size_bytes": 15_122,
        "sha256": "c939d13ee046e1aade81cceb33156d053ab3e4c9cddd2de307efa3e05b42d314",
        "tokens": (
            b"OpenSSL 3.0.13",
            b"NCurses 5.9",
            b"XZ 5.2.3",
            b"Tcl 8.6.13",
            b"Tk 8.6.13",
        ),
    }
)

MACOS_PACK_PYTHON_RUNTIME_DYLIBS: Final[frozenset[str]] = frozenset(
    {"lib/libpython3.11.dylib"}
)

MACOS_NATIVE_LICENSES: Final[Mapping[str, MacOSNativeLicenseText]] = MappingProxyType(
    {
        "openssl": MacOSNativeLicenseText(
            "licenses/native/openssl-3.0.13-LICENSE.txt",
            "ecorex/release/licenses/macos-native/openssl-3.0.13-LICENSE.txt",
            10_175,
            "7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a",
            "https://www.openssl.org/source/openssl-3.0.13.tar.gz",
            "88525753f79d3bec27d2fa7c66aa0b92b3aa9498dafd93d7cfa4b3780cdae313",
            "openssl-3.0.13/LICENSE.txt",
            "upstream archive",
        ),
        "ncurses": MacOSNativeLicenseText(
            "licenses/native/ncurses-5.9-README-license.txt",
            "ecorex/release/licenses/macos-native/ncurses-5.9-README-license.txt",
            9_870,
            "c6842b0dc7af8a79a835690046a55899c62c164aec1342ad9acf933543dfe8f5",
            "https://ftp.gnu.org/gnu/ncurses/ncurses-5.9.tar.gz",
            "9046298fb440324c9d4135ecea7879ffed8546dd1b58e59430ea07a4633f563b",
            "ncurses-5.9/README",
            "upstream archive; CPython macOS BuildScript applies 20120616 patch",
        ),
        "tcl": MacOSNativeLicenseText(
            "licenses/native/tcl-8.6.13-license.terms",
            "ecorex/release/licenses/macos-native/tcl-8.6.13-license.terms",
            2_255,
            "c0a69a2bfd757361ec7e6143973b103c90409316b49e9c88db26ad6388e79f16",
            "https://prdownloads.sourceforge.net/tcl/tcl8.6.13-src.tar.gz",
            "43a1fae7412f61ff11de2cfd05d28cfc3a73762f354a417c62370a54e2caf066",
            "tcl8.6.13/license.terms",
            "CPython 3.11.9 macOS BuildScript checksum and internal path",
        ),
        "tk": MacOSNativeLicenseText(
            "licenses/native/tk-8.6.13-license.terms",
            "ecorex/release/licenses/macos-native/tk-8.6.13-license.terms",
            2_267,
            "2cde822b93ca16ae535c954b7dfe658b4ad10df2a193628d1b358f1765e8b198",
            "https://prdownloads.sourceforge.net/tcl/tk8.6.13-src.tar.gz",
            "2e65fa069a23365440a3c56c556b8673b5e32a283800d8d9b257e3f584ce0675",
            "tk8.6.13/license.terms",
            "CPython 3.11.9 macOS BuildScript checksum and internal path",
        ),
    }
)

MACOS_NATIVE_COMPONENTS: Final[Mapping[str, MacOSNativeComponent]] = MappingProxyType(
    {
        "libcrypto.3.dylib": MacOSNativeComponent(
            "OpenSSL",
            "3.0.13",
            "Apache-2.0",
            "be1a9bf786f1ae89a708afa5e5b3188caa20c46ee9bae5df242b90ef63c64ccd",
            b"OpenSSL 3.0.13",
            "openssl",
        ),
        "libssl.3.dylib": MacOSNativeComponent(
            "OpenSSL",
            "3.0.13",
            "Apache-2.0",
            "22f984c4947e9ea11528ad86d219f145ae9cd45983e3850d34d781d1b38ce5d6",
            b"OpenSSL 3.0.13",
            "openssl",
        ),
        "libformw.5.dylib": MacOSNativeComponent(
            "NCurses",
            "5.9",
            "LicenseRef-NCurses",
            "04a2465d90c2e7239717154417d286325ba3ff3cbf3882dd4e7dba0b1a6061b3",
            b"NCurses 5.9",
            "ncurses",
        ),
        "libmenuw.5.dylib": MacOSNativeComponent(
            "NCurses",
            "5.9",
            "LicenseRef-NCurses",
            "a1a87a584793c6567ebb2e01d2c6ba6b62bc4bd8baff23d128cac32853bd599a",
            b"NCurses 5.9",
            "ncurses",
        ),
        "libncursesw.5.dylib": MacOSNativeComponent(
            "NCurses",
            "5.9",
            "LicenseRef-NCurses",
            "cfa3de54c956f350ece1f9bb3e213958f52d5858b4d68de659b6b513afe2c8e5",
            b"NCurses 5.9",
            "ncurses",
        ),
        "libpanelw.5.dylib": MacOSNativeComponent(
            "NCurses",
            "5.9",
            "LicenseRef-NCurses",
            "489aa1f2fb7753c92e4ed9100ed2c81d91a743284302a7c8a5f6dfa75fb63851",
            b"NCurses 5.9",
            "ncurses",
        ),
        "libtcl8.6.dylib": MacOSNativeComponent(
            "Tcl",
            "8.6.13",
            "TCL",
            "e69ec0775c10545c749f1544279d0edb2534c05a90b1f493fc6d9a4dc8fe4e8d",
            b"Tcl 8.6.13",
            "tcl",
        ),
        "libtk8.6.dylib": MacOSNativeComponent(
            "Tk",
            "8.6.13",
            "TCL",
            "c5251c11109e3948acff9866e27a32921430c5639ca092263aa9dff3de2d9f34",
            b"Tk 8.6.13",
            "tk",
        ),
    }
)


__all__ = [
    "MACOS_NATIVE_COMPONENTS",
    "MACOS_NATIVE_LICENSES",
    "MACOS_PACK_PYTHON_RUNTIME_DYLIBS",
    "PYTHON_MACOS_DISTRIBUTION",
    "PYTHON_MACOS_LICENSE",
    "MacOSNativeComponent",
    "MacOSNativeLicenseText",
]
