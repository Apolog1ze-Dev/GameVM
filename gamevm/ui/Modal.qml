// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Layouts
import "."

// A question in front of the page: dims the window, closes on Escape or a click beside it.
Rectangle {
    id: modal
    default property alias content: col.data
    property int cardWidth: 540
    function open() { visible = true; card.forceActiveFocus() }
    function close() { visible = false }
    parent: Api.root
    anchors.fill: parent
    z: 100
    visible: false
    color: "#66171A21"
    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        onClicked: modal.close()
        onWheel: function(wheel) { wheel.accepted = true }
    }
    Rectangle {
        id: card
        anchors.centerIn: parent
        width: Math.min(modal.cardWidth, modal.width - 64)
        height: col.implicitHeight + 56
        radius: 16
        color: Theme.card
        border.color: Theme.line
        focus: modal.visible
        Keys.onEscapePressed: modal.close()
        Accessible.role: Accessible.Dialog
        MouseArea { anchors.fill: parent }
        ColumnLayout {
            id: col
            anchors.fill: parent
            anchors.margins: 28
            spacing: 14
        }
    }
}
