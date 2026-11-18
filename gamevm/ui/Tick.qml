// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import "."

// A round mark: done (check), current (number), or waiting (outlined number).
Rectangle {
    id: tick
    property string state_: "done"   // done | current | waiting | busy
    property string label: ""
    property int size: 22
    implicitWidth: size
    implicitHeight: size
    radius: size / 2
    color: state_ === "done" ? Theme.okBg : state_ === "current" ? Theme.primary : "transparent"
    border.width: state_ === "waiting" || state_ === "busy" ? 1.5 : 0
    border.color: "#8A93A3"
    Text {
        anchors.centerIn: parent
        text: tick.state_ === "done" ? "✓" : tick.label
        font.family: Theme.font
        font.pixelSize: tick.state_ === "done" ? tick.size * 0.6 : 13
        font.weight: Font.DemiBold
        color: tick.state_ === "done" ? Theme.okText : tick.state_ === "current" ? "#FFFFFF" : Theme.text2
    }
}
