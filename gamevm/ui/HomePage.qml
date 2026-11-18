// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "."

// Home: what Windows is doing, the one thing to do about it, what needs the user, and what
// gamevm took care of without asking.
Page {
    id: page
    readonly property string st: Api.vm.state                 // off | starting | running | stopping
    readonly property bool igpu: Api.vm.mode === "igpu"
    readonly property bool wide: width >= 880

    function runAction(action, item, remember) {
        Api.act(action.id, { device: item.device || "", remember: remember })
    }

    function start() {
        Api.act("start-preview", {}, function(r) {
            if (!r.ok)
                return
            if (r.data.problems.length === 0 && !r.data.confirm)
                Api.act("start", {})
            else
                startDialog.show(r.data)
        })
    }

    Connections {
        target: Api
        function onReadyChanged() { if (Api.ready && Api.arg("--open", "") === "start") page.start() }
    }

    // ---------------------------------------------------------------- state
    Card {
        Layout.fillWidth: true
        pad: 28
        RowLayout {
            Layout.fillWidth: true
            spacing: 28
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 12
                Pill {
                    text: page.st === "running" ? "Running" : page.st === "starting" ? "Starting"
                        : page.st === "stopping" ? "Shutting down" : "Off"
                    tone: page.st === "off" ? "grey" : "win"
                }
                H {
                    Layout.fillWidth: true
                    size: 34
                    text: page.st === "running" ? (page.igpu ? "Windows is running on your main monitor" : "Windows is running")
                        : page.st === "starting" ? "Windows is starting"
                        : page.st === "stopping" ? "Windows is shutting down" : "Windows is off"
                }
                T {
                    Layout.fillWidth: true
                    Layout.maximumWidth: 540
                    color: Theme.text2
                    text: page.st === "running"
                            ? (page.igpu ? "Your Linux desktop moved to the side monitor. It comes back by itself when Windows shuts down."
                                         : "Your Linux desktop is closed meanwhile. It comes back when Windows shuts down.")
                        : page.st === "starting"
                            ? (page.igpu ? "The graphics card is going to Windows and your desktop to the side monitor. Apps that cannot move close and reopen."
                                         : "Your Linux desktop closes now. Linux keeps running in the background.")
                        : page.st === "stopping" ? "Everything it had is coming back to Linux."
                        : (page.igpu ? "Starting it gives the graphics card and your main monitor to Windows. Your Linux desktop moves to the side monitor."
                                     : "Starting it closes your Linux desktop until Windows shuts down. Linux keeps running in the background.")
                }
                Flow {
                    Layout.fillWidth: true
                    Layout.topMargin: 6
                    spacing: 10
                    Btn {
                        visible: page.st === "off"
                        kind: "primary"
                        text: "Start Windows"
                        enabled: !Api.busy.active
                        onClicked: page.start()
                    }
                    Btn {
                        visible: page.st === "running"
                        kind: "primary"
                        text: "Shut down Windows"
                        enabled: !Api.busy.active
                        onClicked: Api.act("stop", {})
                    }
                    Btn {
                        visible: page.st === "running" && Api.vm.lookingGlass
                        text: "Show Windows in a window"
                        onClicked: Api.act("lg", {})
                    }
                }
                // What gamevm is saying while it works.
                T {
                    Layout.fillWidth: true
                    visible: Api.busy.active && Api.busy.lines.length > 0
                    text: Api.busy.lines.length > 0 ? Api.busy.lines[Api.busy.lines.length - 1] : ""
                    font.pixelSize: 13
                    color: Theme.text2
                    elide: Text.ElideRight
                    maximumLineCount: 2
                }
                Btn {
                    visible: page.st === "running" || page.st === "stopping"
                    kind: "link"
                    text: "Windows does not respond? Switch it off"
                    onClicked: killDialog.open()
                }
            }
            // Who is on which monitor.
            Row {
                visible: page.igpu && page.wide
                spacing: 22
                Layout.alignment: Qt.AlignVCenter
                Accessible.role: Accessible.Graphic
                Accessible.name: page.st === "off" ? "Both monitors show Linux"
                                                   : "The main monitor shows Windows, the side monitor shows Linux"
                Column {
                    anchors.bottom: parent.bottom
                    Rectangle {
                        width: 196; height: 116; radius: 10
                        border.width: 3; border.color: Theme.text
                        color: page.st === "off" ? Theme.linBg : Theme.winBg
                        H {
                            anchors.centerIn: parent
                            size: 18
                            text: page.st === "off" ? "Linux" : "Windows"
                            color: page.st === "off" ? Theme.linText : Theme.winText
                        }
                    }
                    Rectangle { width: 14; height: 14; color: Theme.text; anchors.horizontalCenter: parent.horizontalCenter }
                    Rectangle { width: 76; height: 5; radius: 3; color: Theme.text; anchors.horizontalCenter: parent.horizontalCenter }
                    T { text: "Main monitor"; font.pixelSize: 13; color: Theme.text2; topPadding: 8; anchors.horizontalCenter: parent.horizontalCenter }
                }
                Column {
                    anchors.bottom: parent.bottom
                    Rectangle {
                        width: 140; height: 86; radius: 10
                        border.width: 3; border.color: Theme.text
                        color: Theme.linBg
                        H { anchors.centerIn: parent; size: 16; text: "Linux"; color: Theme.linText }
                    }
                    Rectangle { width: 12; height: 12; color: Theme.text; anchors.horizontalCenter: parent.horizontalCenter }
                    Rectangle { width: 60; height: 5; radius: 3; color: Theme.text; anchors.horizontalCenter: parent.horizontalCenter }
                    T { text: "Side monitor"; font.pixelSize: 13; color: Theme.text2; topPadding: 8; anchors.horizontalCenter: parent.horizontalCenter }
                }
            }
        }
    }

    // ---------------------------------------------------------------- needs you / taken care of
    GridLayout {
        Layout.fillWidth: true
        columns: page.wide ? 2 : 1
        columnSpacing: 20
        rowSpacing: 20

        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 400
            Layout.alignment: Qt.AlignTop
            RowLayout {
                spacing: 10
                H { text: "Needs you" }
                Rectangle {
                    visible: Api.needs.length > 0
                    implicitWidth: Math.max(24, count.implicitWidth + 14)
                    implicitHeight: 24
                    radius: 12
                    color: Theme.primary
                    Text {
                        id: count
                        anchors.centerIn: parent
                        text: Api.needs.length
                        color: "#FFFFFF"
                        font.family: Theme.font
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                    }
                }
            }
            T {
                Layout.fillWidth: true
                visible: Api.needs.length === 0
                color: Theme.text2
                text: "Nothing right now."
            }
            Repeater {
                model: Api.needs
                delegate: ColumnLayout {
                    id: need
                    required property var modelData
                    required property int index
                    Layout.fillWidth: true
                    spacing: 6
                    Rectangle {
                        visible: need.index > 0
                        Layout.fillWidth: true
                        Layout.topMargin: 4
                        Layout.bottomMargin: 6
                        implicitHeight: 1
                        color: Theme.line
                    }
                    T { Layout.fillWidth: true; text: need.modelData.title; font.pixelSize: 16; font.weight: Font.DemiBold }
                    T {
                        Layout.fillWidth: true
                        visible: text !== ""
                        text: need.modelData.text || ""
                        font.pixelSize: 14
                        color: Theme.text2
                    }
                    Flow {
                        Layout.fillWidth: true
                        Layout.topMargin: 4
                        spacing: 10
                        Btn {
                            kind: need.modelData.secondary ? "secondary" : "primary"
                            text: need.modelData.primary.label
                            onClicked: page.runAction(need.modelData.primary, need.modelData, remember.checked)
                        }
                        Btn {
                            visible: !!need.modelData.secondary
                            text: need.modelData.secondary ? need.modelData.secondary.label : ""
                            onClicked: page.runAction(need.modelData.secondary, need.modelData, remember.checked)
                        }
                    }
                    Check {
                        id: remember
                        visible: !!need.modelData.remember
                        Layout.fillWidth: true
                        checked: true
                        text: "Remember this, and stop asking"
                    }
                    T {
                        Layout.fillWidth: true
                        visible: text !== ""
                        text: need.modelData.note || ""
                        font.pixelSize: 13
                        color: Theme.text2
                    }
                }
            }
            Item { Layout.fillHeight: true }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
            T {
                Layout.fillWidth: true
                font.pixelSize: 13
                color: Theme.text2
                text: "Only choices that are yours to make show up here. Everything else happens without asking."
            }
        }

        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 600
            Layout.alignment: Qt.AlignTop
            spacing: 14
            H { text: "Taken care of" }
            Repeater {
                model: Api.done
                delegate: RowLayout {
                    id: row
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 12
                    Tick {
                        Layout.alignment: Qt.AlignTop
                        state_: row.modelData.ok ? "done" : "busy"
                        label: "…"
                    }
                    T {
                        Layout.fillWidth: true
                        font.pixelSize: 14
                        textFormat: Text.StyledText
                        text: "<b>" + row.modelData.title + ".</b> <font color=\"" + Theme.text2 + "\">" + row.modelData.text + "</font>"
                    }
                }
            }
            Item { Layout.fillHeight: true }
        }
    }

    // ---------------------------------------------------------------- things to open
    Flow {
        Layout.fillWidth: true
        spacing: 10
        T {
            text: "Open"
            font.pixelSize: 14
            font.weight: Font.DemiBold
            color: Theme.text2
            height: 44
            verticalAlignment: Text.AlignVCenter
        }
        Btn { kind: "chip"; text: "Windows' drives"; onClicked: Api.act("files", {}) }
        Btn { kind: "chip"; text: "Send files to Windows"; onClicked: Api.page = "sharing" }
        Btn { kind: "chip"; text: "What Windows gets"; onClicked: Api.page = "resources" }
    }

    // ---------------------------------------------------------------- before starting
    Modal {
        id: startDialog
        property var info: ({ problems: [], closing: [], reopen: [], mode: "igpu" })
        readonly property var stayClosed: info.closing.filter(function(a) { return info.reopen.indexOf(a) < 0 })
        function show(data) { info = data; dontAsk.checked = false; open() }
        cardWidth: 560
            H {
                Layout.fillWidth: true
                size: 24
                text: startDialog.info.problems.length > 0 ? "Windows cannot start yet" : "Start Windows?"
            }
            Repeater {
                model: startDialog.info.problems
                delegate: T {
                    required property string modelData
                    Layout.fillWidth: true
                    text: modelData
                    font.pixelSize: 14
                }
            }
            T {
                Layout.fillWidth: true
                visible: startDialog.info.problems.length === 0
                color: Theme.text2
                font.pixelSize: 14
                text: startDialog.info.mode === "igpu"
                      ? "Your desktop moves to the side monitor. Most KDE apps stay open; other apps close for a moment."
                      : "Your Linux desktop closes until Windows shuts down. Save your work first."
            }
            T {
                Layout.fillWidth: true
                visible: startDialog.info.problems.length === 0 && startDialog.info.reopen.length > 0
                font.pixelSize: 14
                textFormat: Text.StyledText
                text: "<b>Close and reopen by themselves:</b> " + startDialog.info.reopen.join(", ")
            }
            T {
                Layout.fillWidth: true
                visible: startDialog.info.problems.length === 0 && startDialog.stayClosed.length > 0
                font.pixelSize: 14
                textFormat: Text.StyledText
                text: "<b>Close and stay closed:</b> " + startDialog.stayClosed.join(", ")
            }
            Check {
                id: dontAsk
                Layout.fillWidth: true
                visible: startDialog.info.problems.length === 0
                text: "Do not ask next time"
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 4
                spacing: 10
                Item { Layout.fillWidth: true }
                Btn { text: startDialog.info.problems.length > 0 ? "Close" : "Cancel"; onClicked: startDialog.close() }
                Btn {
                    visible: startDialog.info.problems.length === 0
                    kind: "primary"
                    text: "Start Windows"
                    onClicked: {
                        startDialog.close()
                        Api.act("start", { confirm: !dontAsk.checked })
                    }
                }
            }
    }

    Modal {
        id: killDialog
        cardWidth: 520
            H { Layout.fillWidth: true; size: 24; text: "Switch Windows off?" }
            T {
                Layout.fillWidth: true
                color: Theme.text2
                font.pixelSize: 14
                text: "This is like pulling the plug: anything not saved in Windows is lost. Use it only when Windows no longer reacts."
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                Item { Layout.fillWidth: true }
                Btn { text: "Cancel"; onClicked: killDialog.close() }
                Btn { kind: "danger"; text: "Switch it off"; onClicked: { killDialog.close(); Api.act("kill", {}) } }
            }
    }
}
