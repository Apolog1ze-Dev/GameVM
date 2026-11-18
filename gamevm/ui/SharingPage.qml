// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import QtQuick.Layouts
import QtQuick.Shapes
import "."

// Sharing: send files to Windows (drop them here), see what went and what came, open Windows'
// drives. The clipboard link and the "Send to Linux" entry in Windows are set up by gamevm.
Page {
    id: page
    readonly property var sh: Api.sharing
    readonly property bool wide: width >= 900

    function send(urls) {
        var paths = []
        for (var i = 0; i < urls.length; i++)
            paths.push(urls[i].toString())
        if (paths.length > 0)
            Api.act("send-files", { paths: paths })
    }

    function size(bytes) {
        if (bytes >= 1e9)
            return (bytes / 1e9).toFixed(1) + " GB"
        if (bytes >= 1e6)
            return (bytes / 1e6).toFixed(1) + " MB"
        if (bytes >= 1e3)
            return Math.round(bytes / 1e3) + " kB"
        return bytes + " bytes"
    }

    function stateText(t) {
        if (t.dir === "from")
            return "Received"
        if (t.state === "arrived")
            return "Arrived in Windows"
        if (t.state === "sending")
            return t.progress > 0 ? "Arriving, " + Math.round(t.progress * 100) + " %" : "Sending ..."
        return "Sent"
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: 6
        H { Layout.fillWidth: true; size: 30; text: "Sharing with Windows" }
        T {
            Layout.fillWidth: true
            Layout.maximumWidth: 680
            color: Theme.text2
            text: page.sh.state === "running"
                  ? (page.sh.linked ? "Linked" + (page.sh.device ? " with " + page.sh.device : "")
                                      + ". Text you copy on one side can be pasted on the other; files go through this page."
                                    : "Windows is running. gamevm is linking the two; nothing for you to do.")
                  : "Windows is off, so its drives belong to Linux. Sending and the shared clipboard work while Windows runs."
        }
    }

    GridLayout {
        Layout.fillWidth: true
        columns: page.wide ? 2 : 1
        columnSpacing: 16
        rowSpacing: 16

        // ------------------------------------------------------------ to Windows
        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 480
            pad: 20
            H { text: "Send to Windows" }
            Item {
                id: zone
                property bool over: false
                Layout.fillWidth: true
                implicitHeight: 150
                opacity: page.sh.canSend ? 1 : 0.55
                Shape {
                    anchors.fill: parent
                    ShapePath {
                        strokeColor: zone.over ? Theme.primary : "#8A93A3"
                        strokeWidth: 2
                        fillColor: zone.over ? Theme.primarySoft : Theme.bg
                        strokeStyle: ShapePath.DashLine
                        dashPattern: [4, 3]
                        PathRectangle { x: 1; y: 1; width: zone.width - 2; height: zone.height - 2; radius: 12 }
                    }
                }
                ColumnLayout {
                    anchors.centerIn: parent
                    spacing: 10
                    T {
                        Layout.alignment: Qt.AlignHCenter
                        text: zone.over ? "Let go to send" : "Drop files here"
                        font.pixelSize: 16
                        font.weight: Font.DemiBold
                    }
                    Btn {
                        Layout.alignment: Qt.AlignHCenter
                        kind: "primary"
                        text: "Choose files ..."
                        enabled: page.sh.canSend
                        onClicked: fileDialog.open()
                    }
                }
                DropArea {
                    anchors.fill: parent
                    enabled: page.sh.canSend
                    onEntered: function(drag) { drag.accept(Qt.CopyAction); zone.over = true }
                    onExited: zone.over = false
                    onDropped: function(drop) {
                        zone.over = false
                        if (drop.hasUrls)
                            page.send(drop.urls)
                    }
                }
            }
            T {
                Layout.fillWidth: true
                font.pixelSize: 14
                color: Theme.text2
                text: page.sh.canSend
                      ? "They arrive in the Downloads folder in Windows" + (page.sh.winDownloads ? " (" + page.sh.winDownloads + ")." : ".")
                      : page.sh.why
            }
            Item { Layout.fillHeight: true }
        }

        // ------------------------------------------------------------ from Windows
        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 480
            pad: 20
            H { text: "From Windows" }
            T {
                Layout.fillWidth: true
                font.pixelSize: 14
                textFormat: Text.StyledText
                text: "In Windows, right-click a file and choose <b>Send to &gt; Linux (gamevm)</b>. Or open <b>Send to Linux</b> "
                      + "from the Start menu and drop files on its window."
            }
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Tick {
                    Layout.alignment: Qt.AlignTop
                    state_: page.sh.winTool ? "done" : "busy"
                    label: "…"
                }
                T {
                    Layout.fillWidth: true
                    font.pixelSize: 14
                    color: Theme.text2
                    text: page.sh.winTool ? "gamevm added both to Windows."
                                          : "gamevm adds both by itself the next time Windows runs with this window open."
                }
            }
            T {
                Layout.fillWidth: true
                font.pixelSize: 14
                color: Theme.text2
                text: "They arrive here in " + page.sh.downloads + "."
            }
            Btn { text: "Open Downloads"; onClicked: Api.act("open-downloads", {}) }
            Item { Layout.fillHeight: true }
        }
    }

    // ---------------------------------------------------------------- what went and what came
    Card {
        Layout.fillWidth: true
        pad: 20
        spacing: 0
        RowLayout {
            Layout.fillWidth: true
            Layout.bottomMargin: 10
            H { text: "Activity"; Layout.fillWidth: true }
            Btn {
                visible: page.sh.transfers.length > 0
                kind: "link"
                text: "Clear the list"
                onClicked: Api.act("clear-transfers", {})
            }
        }
        T {
            Layout.fillWidth: true
            visible: page.sh.transfers.length === 0
            color: Theme.text2
            font.pixelSize: 14
            text: "Nothing sent or received yet. Files show up here as they go, with where they arrived."
        }
        Repeater {
            model: page.sh.transfers
            delegate: ColumnLayout {
                id: row
                required property var modelData
                Layout.fillWidth: true
                spacing: 0
                Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
                RowLayout {
                    Layout.fillWidth: true
                    Layout.topMargin: 10
                    Layout.bottomMargin: 10
                    spacing: 12
                    Pill {
                        Layout.alignment: Qt.AlignTop
                        Layout.preferredWidth: 110
                        text: row.modelData.dir === "to" ? "To Windows" : "From Windows"
                        tone: row.modelData.dir === "to" ? "win" : "linux"
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2
                        T {
                            Layout.fillWidth: true
                            text: row.modelData.name
                            font.weight: Font.DemiBold
                            wrapMode: Text.WrapAnywhere
                        }
                        T {
                            Layout.fillWidth: true
                            font.pixelSize: 13
                            color: Theme.text2
                            wrapMode: Text.WrapAnywhere
                            text: page.size(row.modelData.size) + (row.modelData.where ? ", " + row.modelData.where : "")
                        }
                        // how far a file that is still arriving has come
                        Rectangle {
                            visible: row.modelData.state === "sending" && row.modelData.progress > 0
                            Layout.fillWidth: true
                            Layout.topMargin: 4
                            implicitHeight: 6
                            radius: 3
                            color: Theme.line
                            Rectangle {
                                width: parent.width * row.modelData.progress
                                height: parent.height
                                radius: 3
                                color: Theme.primary
                            }
                        }
                    }
                    ColumnLayout {
                        Layout.alignment: Qt.AlignTop
                        spacing: 2
                        T {
                            Layout.alignment: Qt.AlignRight
                            font.pixelSize: 14
                            font.weight: Font.Medium
                            color: row.modelData.state === "arrived" || row.modelData.dir === "from" ? Theme.okText : Theme.text
                            text: page.stateText(row.modelData)
                        }
                        T {
                            Layout.alignment: Qt.AlignRight
                            font.pixelSize: 13
                            color: Theme.text2
                            text: row.modelData.when
                        }
                    }
                    Btn {
                        visible: row.modelData.dir === "from"
                        Layout.alignment: Qt.AlignTop
                        kind: "chip"
                        text: "Show"
                        Accessible.name: "Show " + row.modelData.name + " in its folder"
                        onClicked: Api.act("show-transfer", { id: row.modelData.id })
                    }
                }
            }
        }
    }

    GridLayout {
        Layout.fillWidth: true
        columns: page.wide ? 2 : 1
        columnSpacing: 16
        rowSpacing: 16

        // ------------------------------------------------------------ drives
        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 480
            pad: 20
            H { text: "Windows' drives" }
            T {
                Layout.fillWidth: true
                font.pixelSize: 14
                color: Theme.text2
                text: page.sh.state === "running"
                      ? "Whole folders and big files: open the drive and copy. The first time, the file manager asks for your Windows user name and password, and can remember them."
                      : "Windows is off, so its drives are ordinary folders on Linux."
            }
            Flow {
                Layout.fillWidth: true
                spacing: 10
                Repeater {
                    model: page.sh.drives
                    delegate: Btn {
                        required property var modelData
                        kind: "chip"
                        text: "Open " + modelData.label
                        onClicked: Api.act("open-drive", { url: modelData.url })
                    }
                }
            }
            Item { Layout.fillHeight: true }
        }

        // ------------------------------------------------------------ clipboard
        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 480
            pad: 20
            H { text: "Clipboard" }
            RowLayout {
                Layout.fillWidth: true
                spacing: 12
                Tick {
                    Layout.alignment: Qt.AlignTop
                    state_: page.sh.linked ? "done" : "busy"
                    label: "…"
                }
                T {
                    Layout.fillWidth: true
                    font.pixelSize: 14
                    text: !page.sh.link ? "The private link between Linux and Windows is switched off (Resources, Network)."
                        : page.sh.linked ? "Shared. Copy text on one side, paste it on the other."
                        : page.sh.state === "running" ? "Linking. gamevm does it by itself; leave the pairing request it shows alone."
                        : "Shared while Windows runs."
                }
            }
            T {
                Layout.fillWidth: true
                font.pixelSize: 14
                color: Theme.text2
                text: "Text only. A copied file cannot be pasted across: send it above instead."
            }
            Item { Layout.fillHeight: true }
        }
    }

    FileDialog {
        id: fileDialog
        title: "Send to Windows"
        fileMode: FileDialog.OpenFiles
        onAccepted: page.send(selectedFiles)
    }
}
