// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "."

// Help: the checks in full, the guides, and what to do when something went wrong.
Page {
    id: page
    maxWidth: 860
    readonly property var d: Api.help.doctor

    ColumnLayout {
        Layout.fillWidth: true
        spacing: 6
        H { Layout.fillWidth: true; size: 30; text: "Help" }
        T { Layout.fillWidth: true; color: Theme.text2; text: "The checks in full, the guides, and repairs." }
    }

    Card {
        Layout.fillWidth: true
        pad: 20
        spacing: 10
        RowLayout {
            Layout.fillWidth: true
            spacing: 12
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                H { text: "Checks" }
                T {
                    Layout.fillWidth: true
                    font.pixelSize: 14
                    color: Theme.text2
                    text: page.d.running || !page.d.ran ? "Running now ..."
                        : (page.d.fails === 0 ? "No problems" : page.d.fails + " problem(s)")
                          + (page.d.warns ? ", " + page.d.warns + " notes" : "") + ". Last run at " + page.d.when
                          + ". They run by themselves; a problem shows up on the Home page."
                }
            }
            Btn { text: "Check again"; enabled: !page.d.running; onClicked: Api.act("run-checks", {}) }
        }
        Repeater {
            model: page.d.sections
            delegate: ColumnLayout {
                id: sec
                required property var modelData
                readonly property var notes: modelData.items.filter(function(i) { return i.level !== "ok" })
                Layout.fillWidth: true
                spacing: 6
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    Tick { state_: sec.notes.length === 0 ? "done" : "busy"; label: "!" }
                    T { Layout.fillWidth: true; text: sec.modelData.title; font.weight: Font.DemiBold }
                    T {
                        font.pixelSize: 13
                        color: Theme.text2
                        text: (sec.modelData.items.length - sec.notes.length) + " fine"
                              + (sec.notes.length ? ", " + sec.notes.length + " to read" : "")
                    }
                }
                Repeater {
                    model: sec.notes
                    delegate: ColumnLayout {
                        id: note
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.leftMargin: 32
                        spacing: 2
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            Pill {
                                Layout.alignment: Qt.AlignTop
                                text: note.modelData.level === "fail" ? "Problem" : "Note"
                                tone: note.modelData.level === "fail" ? "win" : "grey"
                            }
                            T { Layout.fillWidth: true; font.pixelSize: 14; text: note.modelData.msg }
                        }
                        T {
                            Layout.fillWidth: true
                            visible: text !== ""
                            font.pixelSize: 13
                            color: Theme.text2
                            text: note.modelData.hint
                        }
                    }
                }
            }
        }
    }

    Card {
        Layout.fillWidth: true
        pad: 20
        spacing: 10
        H { text: "Guides" }
        Flow {
            Layout.fillWidth: true
            spacing: 10
            Btn { kind: "chip"; text: "Daily use"; onClicked: Api.act("open-doc", { name: "03-daily-use.md" }) }
            Btn { kind: "chip"; text: "Windows side"; onClicked: Api.act("open-doc", { name: "02-windows.md" }) }
            Btn { kind: "chip"; text: "Games and anti-cheat"; onClicked: Api.act("open-doc", { name: "05-games-and-anti-cheat.md" }) }
            Btn { kind: "chip"; text: "Performance"; onClicked: Api.act("open-doc", { name: "06-performance.md" }) }
            Btn { kind: "chip"; text: "Troubleshooting"; onClicked: Api.act("open-doc", { name: "04-troubleshooting.md" }) }
        }
    }

    Card {
        Layout.fillWidth: true
        pad: 20
        spacing: 10
        H { text: "If something went wrong" }
        T {
            Layout.fillWidth: true
            font.pixelSize: 14
            color: Theme.text2
            text: "After a crash or a power cut while Windows was running, Repair gives the graphics card, the disks and "
                  + "the processor cores back to Linux. It asks for your password."
        }
        Flow {
            Layout.fillWidth: true
            spacing: 10
            Btn { text: "Repair after a crash"; enabled: !Api.busy.active; onClicked: Api.act("recover", {}) }
            Btn { text: "Open the log"; onClicked: Api.act("open-log", {}) }
            Btn { text: "Open the settings file"; onClicked: Api.act("open-config", {}) }
        }
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: logText.implicitHeight + 24
            radius: 10
            color: Theme.bg
            border.color: Theme.line
            Text {
                id: logText
                anchors.fill: parent
                anchors.margins: 12
                text: Api.help.log.slice(-8).join("\n")
                font.family: Theme.mono
                font.pixelSize: 12
                color: Theme.text2
                wrapMode: Text.WrapAnywhere
                textFormat: Text.PlainText
            }
        }
    }

    T {
        Layout.fillWidth: true
        font.pixelSize: 13
        color: Theme.text2
        text: "gamevm " + (Api.s.version || "")
    }
}
