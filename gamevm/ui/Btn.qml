// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import "."

// kind: primary | secondary | chip | link | danger
Button {
    id: b
    property string kind: "secondary"
    readonly property bool flat_: kind === "link"
    implicitHeight: 44
    leftPadding: flat_ ? 0 : (kind === "chip" ? 16 : 20)
    rightPadding: leftPadding
    topPadding: 0
    bottomPadding: 0
    font.family: Theme.font
    font.pixelSize: kind === "chip" || flat_ ? 14 : 15
    font.weight: kind === "primary" || flat_ || kind === "danger" ? Font.DemiBold : Font.Medium
    opacity: enabled ? 1 : 0.45
    contentItem: Text {
        text: b.text
        font: b.font
        color: b.kind === "primary" ? "#FFFFFF" : b.flat_ ? (b.hovered ? Theme.primaryDark : Theme.primary)
             : b.kind === "danger" ? Theme.danger : Theme.text
        horizontalAlignment: b.flat_ ? Text.AlignLeft : Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    background: Rectangle {
        radius: b.kind === "chip" ? height / 2 : 12
        color: b.flat_ ? "transparent"
             : b.kind === "primary" ? (b.down || b.hovered ? Theme.primaryDark : Theme.primary)
             : (b.down || b.hovered ? Theme.hover : Theme.card)
        border.width: b.flat_ ? 0 : (b.visualFocus ? 2 : 1)
        border.color: b.visualFocus ? Theme.primaryDark : b.kind === "primary" ? Theme.primary
                    : b.kind === "danger" ? Theme.danger : Theme.border
    }
    HoverHandler { cursorShape: Qt.PointingHandCursor }
}
