// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import "."

// A drop-down list.
ComboBox {
    id: box
    implicitHeight: 44
    implicitWidth: 190
    font.family: Theme.font
    font.pixelSize: 14
    font.weight: Font.Medium
    leftPadding: 14
    rightPadding: 34
    contentItem: Text {
        text: box.displayText
        font: box.font
        color: Theme.text
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    indicator: Text {
        x: box.width - width - 14
        anchors.verticalCenter: parent.verticalCenter
        text: "▾"
        font.pixelSize: 14
        color: Theme.text2
    }
    background: Rectangle {
        radius: 10
        color: box.hovered ? Theme.hover : Theme.card
        border.width: box.visualFocus ? 2 : 1
        border.color: box.visualFocus ? Theme.primaryDark : Theme.border
    }
    delegate: ItemDelegate {
        id: option
        required property var modelData
        required property int index
        width: box.width
        implicitHeight: 44
        highlighted: box.highlightedIndex === index
        contentItem: Text {
            text: option.modelData
            font: box.font
            color: Theme.text
            verticalAlignment: Text.AlignVCenter
            leftPadding: 4
        }
        background: Rectangle { color: option.highlighted ? Theme.primarySoft : Theme.card }
    }
    popup: Popup {
        y: box.height + 4
        width: box.width
        implicitHeight: contentItem.implicitHeight + 2
        padding: 1
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: box.popup.visible ? box.delegateModel : null
            currentIndex: box.highlightedIndex
        }
        background: Rectangle { radius: 10; color: Theme.card; border.color: Theme.border }
    }
    HoverHandler { cursorShape: Qt.PointingHandCursor }
}
