// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import "."

// Heading.
Text {
    property int size: 20
    font.family: Theme.display
    font.pixelSize: size
    font.weight: Font.DemiBold
    font.letterSpacing: -0.2
    color: Theme.text
    wrapMode: Text.WordWrap
    textFormat: Text.PlainText
    Accessible.role: Accessible.Heading
}
