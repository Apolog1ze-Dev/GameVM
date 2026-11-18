// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Layouts
import "."

Rectangle {
    id: card
    default property alias content: col.data
    property int pad: 22
    property alias spacing: col.spacing
    color: Theme.card
    radius: 16
    border.color: Theme.line
    border.width: 1
    implicitHeight: col.implicitHeight + 2 * pad
    implicitWidth: col.implicitWidth + 2 * pad
    ColumnLayout {
        id: col
        anchors.fill: parent
        anchors.margins: card.pad
        spacing: 12
    }
}
