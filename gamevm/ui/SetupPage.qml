// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "."

// Setup: one list. Each step says who does it. gamevm's steps happen (or are noticed) by
// themselves; the user's steps wait with one button and are noticed when they are done.
Page {
    id: page
    readonly property var steps: Api.setup.steps
    readonly property bool wide: width >= 940
    readonly property var r: Api.resources

    GridLayout {
        Layout.fillWidth: true
        columns: page.wide ? 2 : 1
        columnSpacing: 24
        rowSpacing: 20

        ColumnLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: 680
            spacing: 16
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 6
                H {
                    Layout.fillWidth: true
                    size: 30
                    text: Api.setup.left === 0 ? "gamevm is set up" : "Setting up gamevm"
                }
                T {
                    Layout.fillWidth: true
                    Layout.maximumWidth: 640
                    color: Theme.text2
                    text: Api.setup.left === 0
                          ? "Everything is in place. If something changes, the step it belongs to opens again here."
                          : "gamevm does everything it can by itself. " + Api.setup.yours
                            + " steps need your hands; it waits for those and then carries on."
                }
            }

            ColumnLayout {
              Layout.fillWidth: true
              spacing: 8
              Repeater {
                model: page.steps
                delegate: Rectangle {
                    id: step
                    required property var modelData
                    required property int index
                    readonly property bool current: modelData.state === "current"
                    Layout.fillWidth: true
                    implicitHeight: body.implicitHeight + (current ? 36 : 22)
                    radius: 12
                    color: modelData.state === "waiting" ? "transparent" : Theme.card
                    border.width: current ? 2 : 1
                    border.color: current ? Theme.primary : Theme.line
                    RowLayout {
                        id: body
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.leftMargin: 16
                        anchors.rightMargin: 16
                        spacing: 14
                        Tick {
                            Layout.alignment: step.current ? Qt.AlignTop : Qt.AlignVCenter
                            size: 26
                            state_: step.modelData.state
                            label: step.index + 1
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: step.current ? 8 : 0
                            T {
                                Layout.fillWidth: true
                                visible: !step.current
                                text: step.modelData.title
                                font.weight: Font.DemiBold
                                color: step.modelData.state === "waiting" ? Theme.greyText : Theme.text
                            }
                            H {
                                Layout.fillWidth: true
                                visible: step.current
                                text: (step.modelData.by === "you" ? "Your turn: " : "") + step.modelData.title.charAt(0).toLowerCase()
                                      + step.modelData.title.slice(1)
                                Component.onCompleted: if (step.modelData.by !== "you") text = step.modelData.title
                            }
                            T {
                                Layout.fillWidth: true
                                text: step.modelData.text
                                font.pixelSize: step.current ? 14 : 13
                                color: Theme.text2
                            }
                            Flow {
                                Layout.fillWidth: true
                                visible: step.current && !!step.modelData.action
                                spacing: 10
                                Btn {
                                    kind: "primary"
                                    text: step.modelData.action ? step.modelData.action.label : ""
                                    enabled: !Api.busy.active
                                    onClicked: step.modelData.action.id === "restart" ? restartDialog.open()
                                                                                       : Api.act(step.modelData.action.id, {})
                                }
                                T {
                                    visible: text !== ""
                                    text: step.modelData.note || ""
                                    font.pixelSize: 13
                                    color: Theme.text2
                                    height: 44
                                    verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                        Pill {
                            Layout.alignment: step.current ? Qt.AlignTop : Qt.AlignVCenter
                            text: step.modelData.by === "you" ? "You" : "gamevm"
                            tone: step.modelData.by === "you" ? "primary" : "grey"
                        }
                    }
                }
              }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignTop
            Layout.preferredWidth: 310
            spacing: 16
            Card {
                Layout.fillWidth: true
                pad: 20
                spacing: 8
                H { text: "Chosen for Windows"; size: 18 }
                Repeater {
                    model: [
                        ["Processor", page.r.cores.filter(function(c) { return c.windows }).length + " of " + page.r.cores.length + " cores"],
                        ["Memory", (page.r.mem || 0) + " of " + (page.r.memTotal || 0) + " GiB"],
                        ["Graphics", page.r.gpus.filter(function(g) { return g.role === "windows" }).map(function(g) { return g.name }).join(", ")],
                        ["Disks", page.r.disks.filter(function(d) { return d.role !== "linux" }).length + " (Windows, shared)"]
                    ]
                    delegate: RowLayout {
                        id: line
                        required property var modelData
                        Layout.fillWidth: true
                        spacing: 12
                        T { text: line.modelData[0]; font.pixelSize: 14; color: Theme.text2 }
                        T {
                            Layout.fillWidth: true
                            text: line.modelData[1]
                            font.pixelSize: 14
                            font.weight: Font.Medium
                            horizontalAlignment: Text.AlignRight
                        }
                    }
                }
                Btn { kind: "link"; text: "Change what Windows gets"; onClicked: Api.page = "resources" }
            }
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: safe.implicitHeight + 40
                radius: 16
                color: Theme.okBg
                ColumnLayout {
                    id: safe
                    anchors.fill: parent
                    anchors.margins: 20
                    spacing: 8
                    H { text: "Safe to try"; size: 18; color: Theme.okText }
                    T {
                        Layout.fillWidth: true
                        font.pixelSize: 14
                        color: Theme.okText
                        text: "Every file gamevm edits keeps a backup, and your Windows is started as it is, never converted. The whole setup can be undone."
                    }
                    Btn {
                        kind: "link"
                        text: "Undo the setup"
                        onClicked: undoDialog.open()
                    }
                }
            }
        }
    }

    Modal {
        id: restartDialog
        cardWidth: 520
            H { Layout.fillWidth: true; size: 24; text: "Restart the PC now?" }
            T {
                Layout.fillWidth: true
                color: Theme.text2
                font.pixelSize: 14
                text: "Save your work in other programs first. This window opens again after you log in and carries on with the setup."
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                Item { Layout.fillWidth: true }
                Btn { text: "Later"; onClicked: restartDialog.close() }
                Btn { kind: "primary"; text: "Restart now"; onClicked: { restartDialog.close(); Api.act("restart", {}) } }
            }
    }

    Modal {
        id: undoDialog
        cardWidth: 520
            H { Layout.fillWidth: true; size: 24; text: "Undo the whole setup?" }
            T {
                Layout.fillWidth: true
                color: Theme.text2
                font.pixelSize: 14
                text: "This removes gamevm's hook, units and settings from Linux and restores the files it edited. "
                      + "Windows itself is not touched and still starts natively. A terminal opens and asks for your password."
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                Item { Layout.fillWidth: true }
                Btn { text: "Cancel"; onClicked: undoDialog.close() }
                Btn { kind: "danger"; text: "Undo the setup"; onClicked: { undoDialog.close(); Api.act("uninstall", {}) } }
            }
    }
}
