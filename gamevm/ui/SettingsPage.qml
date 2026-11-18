// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "."

// Settings: how the switch between Linux and Windows behaves. A draft, saved like Resources.
Page {
    id: page
    maxWidth: 760
    readonly property var v: Api.settings

    property bool dirty: false
    property bool confirmStart: true
    property bool reopenApps: true
    property bool restoreWindows: true
    property bool returnAsk: false
    property int shutdownWait: 300
    property bool lgFullscreen: false
    property bool reattach: true
    property string toggle: "ctrl-ctrl"

    function load() {
        confirmStart = v.confirmStart !== false
        reopenApps = !!v.reopenApps
        restoreWindows = !!v.restoreWindows
        returnAsk = !!v.returnAsk
        shutdownWait = v.shutdownWait || 300
        lgFullscreen = !!v.lgFullscreen
        reattach = !!v.reattach
        toggle = v.toggle || "ctrl-ctrl"
        dirty = false
    }

    function save() {
        Api.act("save-settings", { confirmStart: confirmStart, reopenApps: reopenApps, restoreWindows: restoreWindows,
                                   returnAsk: returnAsk, shutdownWait: shutdownWait, lgFullscreen: lgFullscreen,
                                   reattach: reattach, toggle: toggle }, function(res) { if (res.ok) dirty = false })
    }

    Connections {
        target: Api
        function onSChanged() { if (!page.dirty) page.load() }
    }
    Component.onCompleted: load()

    RowLayout {
        Layout.fillWidth: true
        spacing: 16
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 6
            H { Layout.fillWidth: true; size: 30; text: "Settings" }
            T { Layout.fillWidth: true; color: Theme.text2; text: "How the switch between Linux and Windows behaves." }
        }
        Btn {
            kind: "primary"
            text: "Save"
            enabled: page.dirty && !Api.busy.active
            onClicked: page.save()
            Layout.alignment: Qt.AlignBottom
        }
    }

    Card {
        Layout.fillWidth: true
        pad: 20
        spacing: 2
        H { text: "Starting and stopping"; Layout.bottomMargin: 6 }
        Check {
            Layout.fillWidth: true
            text: "Show what will close before Windows starts"
            checked: page.confirmStart
            onToggled: { page.confirmStart = checked; page.dirty = true }
        }
        Check {
            Layout.fillWidth: true
            text: "Reopen my apps after the desktop has moved"
            checked: page.reopenApps
            onToggled: { page.reopenApps = checked; page.dirty = true }
        }
        Check {
            Layout.fillWidth: true
            text: "Put windows back on the monitor and at the place they had"
            checked: page.restoreWindows
            onToggled: { page.restoreWindows = checked; page.dirty = true }
        }
        Check {
            Layout.fillWidth: true
            text: "When Windows shuts down, ask before moving the desktop back to the main monitor"
            checked: page.returnAsk
            onToggled: { page.returnAsk = checked; page.dirty = true }
        }
        RowLayout {
            Layout.fillWidth: true
            Layout.topMargin: 6
            spacing: 12
            T {
                Layout.fillWidth: true
                font.pixelSize: 14
                text: "When the PC is switched off, wait this long for Windows to shut down"
            }
            Pick {
                Accessible.name: "Wait for Windows"
                implicitWidth: 150
                readonly property var values: [60, 120, 300, 600, 840]
                model: ["1 minute", "2 minutes", "5 minutes", "10 minutes", "14 minutes"]
                currentIndex: {
                    var best = 0
                    for (var i = 0; i < values.length; i++)
                        if (Math.abs(values[i] - page.shutdownWait) < Math.abs(values[best] - page.shutdownWait))
                            best = i
                    return best
                }
                onActivated: function(i) { page.shutdownWait = values[i]; page.dirty = true }
            }
        }
    }

    Card {
        Layout.fillWidth: true
        pad: 20
        spacing: 2
        H { text: "Keyboard and mouse"; Layout.bottomMargin: 6 }
        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            T { Layout.fillWidth: true; font.pixelSize: 14; text: "Keys that switch them between Linux and Windows" }
            Pick {
                Accessible.name: "Switch keys"
                implicitWidth: 210
                model: page.v.toggles.map(function(t) { return t.label })
                currentIndex: Math.max(0, page.v.toggles.map(function(t) { return t.id }).indexOf(page.toggle))
                onActivated: function(i) { page.toggle = page.v.toggles[i].id; page.dirty = true }
            }
        }
        Check {
            Layout.fillWidth: true
            text: "Reconnect them to Windows by themselves after unplugging and plugging back in"
            checked: page.reattach
            onToggled: { page.reattach = checked; page.dirty = true }
        }
    }

    Card {
        Layout.fillWidth: true
        visible: Api.vm.mode === "igpu"
        pad: 20
        spacing: 2
        H { text: "Windows in a window"; Layout.bottomMargin: 6 }
        Check {
            Layout.fillWidth: true
            text: "Open it full screen on the side monitor (Right Ctrl + F leaves full screen)"
            checked: page.lgFullscreen
            onToggled: { page.lgFullscreen = checked; page.dirty = true }
        }
    }

    T {
        Layout.fillWidth: true
        font.pixelSize: 13
        color: Theme.text2
        text: "Saving asks for your password. Everything else is in the settings file, which Help can open."
    }
}
