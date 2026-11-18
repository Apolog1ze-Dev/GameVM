// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import "."

// tone: win | linux | ok | grey | primary
Rectangle {
    id: pill
    property string text: ""
    property string tone: "grey"
    implicitHeight: 24
    implicitWidth: label.implicitWidth + 20
    radius: 12
    color: tone === "win" ? Theme.winBg : tone === "linux" ? Theme.linBg : tone === "ok" ? Theme.okBg
         : tone === "primary" ? Theme.primarySoft : Theme.greyBg
    Text {
        id: label
        anchors.centerIn: parent
        text: pill.text
        font.family: Theme.font
        font.pixelSize: 12
        font.weight: Font.DemiBold
        color: pill.tone === "win" ? Theme.winText : pill.tone === "linux" ? Theme.linText : pill.tone === "ok" ? Theme.okText
             : pill.tone === "primary" ? Theme.primaryDark : Theme.greyText
    }
}
