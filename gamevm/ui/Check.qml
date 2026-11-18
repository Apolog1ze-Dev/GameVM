// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import "."

CheckBox {
    id: c
    spacing: 10
    padding: 0
    topPadding: 10
    bottomPadding: 10
    font.family: Theme.font
    font.pixelSize: 14
    indicator: Rectangle {
        implicitWidth: 20
        implicitHeight: 20
        x: 0
        y: c.topPadding + 1
        radius: 5
        color: c.checked ? Theme.primary : Theme.card
        border.width: c.visualFocus ? 2 : 1.5
        border.color: c.checked || c.visualFocus ? Theme.primaryDark : "#8A93A3"
        Text {
            anchors.centerIn: parent
            visible: c.checked
            text: "✓"
            color: "#FFFFFF"
            font.pixelSize: 14
            font.weight: Font.Bold
        }
    }
    contentItem: Text {
        text: c.text
        font: c.font
        color: Theme.text
        wrapMode: Text.WordWrap
        lineHeight: 1.25
        leftPadding: c.indicator.width + c.spacing
        verticalAlignment: Text.AlignVCenter
    }
    HoverHandler { cursorShape: Qt.PointingHandCursor }
}
