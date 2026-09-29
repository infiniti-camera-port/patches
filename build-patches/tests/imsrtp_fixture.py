from __future__ import annotations

from typing import Final

IMSRTP_FILES: Final = {
    "interfaces/Android.bp": """aidl_interface {
    name: "vendor.oplus.hardware.subsys_imsrtp",
    system_ext_specific: true,
    vendor_available: true,
    local_include_dir: "aidl",
    srcs: ["aidl/vendor/oplus/hardware/subsys_imsrtp/*.aidl"],
    stability: "vintf",
    owner: "oplus",

    backend: {
        cpp: {
            enabled: true,
        },
        java: {
            enabled: false,
        },
    },
    imports: [
        "vendor.oplus.hardware.subsys-V5",
    ],
    versions_with_info: [
        {
            version: "1",
            imports: ["vendor.oplus.hardware.subsys-V5"],
        },
    ],
}

aidl_interface {
    name: "vendor.oplus.hardware.subsys",
    system_ext_specific: true,
    odm_available: true,
    local_include_dir: "aidl",
    srcs: ["aidl/vendor/oplus/hardware/subsys_interface/subsys/*.aidl"],
    stability: "vintf",
    owner: "oplus",

    backend: {
        cpp: {
            enabled: true,
        },
        java: {
            enabled: false,
        },
    },
    versions_with_info: [
        {
            version: "1",
            imports: [],
        },
        {
            version: "2",
            imports: [],
        },
        {
            version: "3",
            imports: [],
        },
        {
            version: "4",
            imports: [],
        },
        {
            version: "5",
            imports: [],
        },
    ],
}
"""
}
