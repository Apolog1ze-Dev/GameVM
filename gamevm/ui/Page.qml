// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "."

// A scrolling page with one centred column.
Flickable {
    id: fl
    default property alias content: col.data
    property int maxWidth: 1016
    contentWidth: width
    contentHeight: col.implicitHeight + 52
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar { }
    ColumnLayout {
        id: col
        x: Math.max(32, (fl.width - width) / 2)
        y: 26
        width: Math.min(fl.maxWidth, fl.width - 64)
        spacing: 20
    }
}
