// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
pragma Singleton
import QtQuick

// Colours and type of the gamevm window (the "Guide" design: light, plain, one accent).
QtObject {
    readonly property color bg: "#F5F6F8"
    readonly property color card: "#FFFFFF"
    readonly property color line: "#DFE3EA"
    readonly property color border: "#C9D0DB"
    readonly property color text: "#171A21"
    readonly property color text2: "#4B5565"
    readonly property color primary: "#2B5FD9"
    readonly property color primaryDark: "#1E48AD"
    readonly property color primarySoft: "#EEF2FB"
    readonly property color winBg: "#FFF1E0"      // Windows has it
    readonly property color winText: "#9A4A00"
    readonly property color winLine: "#C96A00"
    readonly property color linBg: "#E6EEFC"      // Linux has it
    readonly property color linText: "#1E48AD"
    readonly property color okBg: "#E3F5EC"
    readonly property color okText: "#0F6B4B"
    readonly property color greyBg: "#EEF0F4"
    readonly property color greyText: "#3A4352"
    readonly property color danger: "#B3261E"
    readonly property color hover: "#F0F2F6"
    readonly property string font: Qt.fontFamilies().indexOf("Inter") >= 0 ? "Inter" : "Noto Sans"
    readonly property string display: Qt.fontFamilies().indexOf("Space Grotesk") >= 0 ? "Space Grotesk" : font
    readonly property string mono: Qt.fontFamilies().indexOf("JetBrains Mono") >= 0 ? "JetBrains Mono" : "monospace"
}
