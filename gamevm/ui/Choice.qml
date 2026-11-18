// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import "."

// One option of an either-or choice: a round mark, a title and a line of explanation.
RadioButton {
    id: c
    property string title: ""
    property string detail: ""
    padding: 0
    topPadding: 10
    bottomPadding: 10
    spacing: 10
    autoExclusive: false
    Accessible.name: title
    indicator: Rectangle {
        implicitWidth: 20
        implicitHeight: 20
        x: 0
        y: c.topPadding + 2
        radius: 10
        color: Theme.card
        border.width: c.visualFocus ? 2.5 : 1.5
        border.color: c.checked || c.visualFocus ? Theme.primaryDark : "#8A93A3"
        Rectangle {
            anchors.centerIn: parent
            width: 10
            height: 10
            radius: 5
            color: Theme.primary
            visible: c.checked
        }
    }
    contentItem: Column {
        leftPadding: c.indicator.width + c.spacing
        spacing: 2
        Text {
            width: parent.width - parent.leftPadding
            text: c.title
            font.family: Theme.font
            font.pixelSize: 15
            font.weight: Font.DemiBold
            color: Theme.text
            wrapMode: Text.WordWrap
        }
        Text {
            width: parent.width - parent.leftPadding
            visible: c.detail !== ""
            text: c.detail
            font.family: Theme.font
            font.pixelSize: 13
            color: Theme.text2
            wrapMode: Text.WordWrap
        }
    }
    HoverHandler { cursorShape: Qt.PointingHandCursor }
}
