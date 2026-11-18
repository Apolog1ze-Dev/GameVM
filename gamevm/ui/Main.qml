// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Window
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "."

// The gamevm window. Started by `gamevm ui`, which also runs the local service the pages talk to
// (Api.qml). Test hooks: --page NAME opens on that page; --shot FILE saves a picture and quits
// (after --delay MS); --open start shows the question asked before Windows starts.
ApplicationWindow {
    id: win
    width: 1180
    height: 860
    minimumWidth: 640
    minimumHeight: 520
    visible: true
    title: "gamevm"
    color: Theme.bg

    readonly property var pages: ["home", "sharing", "resources", "setup", "settings", "help"]
    readonly property var pageNames: ["Home", "Sharing", "Resources", "Set up", "Settings", "Help"]

    Rectangle {
        id: root
        anchors.fill: parent
        color: Theme.bg

        Rectangle {
            id: bar
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            color: Theme.card
            height: 64
            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 1
                color: Theme.line
            }
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 32
                anchors.rightMargin: 32
                spacing: 24
                Text {
                    text: "gamevm"
                    font.family: Theme.display
                    font.pixelSize: 20
                    font.weight: Font.Bold
                    color: Theme.text
                }
                Row {
                    spacing: 4
                    Repeater {
                        model: win.pages.length
                        delegate: Button {
                            id: tab
                            required property int index
                            readonly property bool current: Api.page === win.pages[index]
                            text: win.pageNames[index]
                            implicitHeight: 44
                            leftPadding: 14
                            rightPadding: 14
                            font.family: Theme.font
                            font.pixelSize: 15
                            font.weight: current ? Font.DemiBold : Font.Medium
                            Accessible.name: text
                            onClicked: Api.page = win.pages[index]
                            contentItem: Text {
                                text: tab.text
                                font: tab.font
                                color: tab.current ? Theme.primaryDark : Theme.text2
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                radius: 10
                                color: tab.current ? Theme.primarySoft : tab.hovered ? Theme.hover : "transparent"
                                border.width: tab.visualFocus ? 2 : 0
                                border.color: Theme.primaryDark
                            }
                            HoverHandler { cursorShape: Qt.PointingHandCursor }
                        }
                    }
                }
                Item { Layout.fillWidth: true }
                T {
                    visible: Api.busy.active
                    text: Api.busy.title + " ..."
                    font.pixelSize: 14
                    color: Theme.text2
                    elide: Text.ElideRight
                    wrapMode: Text.NoWrap
                    Layout.maximumWidth: 320
                }
            }
        }

        StackLayout {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: bar.bottom
            anchors.bottom: parent.bottom
            currentIndex: Math.max(0, win.pages.indexOf(Api.page))
            HomePage { }
            SharingPage { }
            ResourcesPage { }
            SetupPage { }
            SettingsPage { }
            HelpPage { }
        }

        // What the last action answered, for a few seconds.
        Rectangle {
            id: toast
            visible: Api.toast !== ""
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 24
            width: Math.min(toastText.implicitWidth + 40, parent.width - 64)
            height: toastText.implicitHeight + 24
            radius: 12
            color: Theme.text
            T {
                id: toastText
                anchors.centerIn: parent
                width: Math.min(implicitWidth, win.width - 104)
                text: Api.toast
                color: "#FFFFFF"
                font.pixelSize: 14
            }
            Timer {
                interval: 6000
                running: toast.visible
                onTriggered: Api.toast = ""
            }
        }

        // The service went away.
        Rectangle {
            visible: Api.error !== ""
            anchors.fill: parent
            color: Theme.bg
            T {
                anchors.centerIn: parent
                width: Math.min(520, parent.width - 64)
                horizontalAlignment: Text.AlignHCenter
                text: Api.error
            }
        }
    }

    // --shot FILE: save a picture of the window once the first state has arrived, then quit.
    Timer {
        interval: parseInt(Api.arg("--delay", "900"))
        running: Api.ready && Api.arg("--shot", "") !== ""
        onTriggered: root.grabToImage(function(result) {
            result.saveToFile(Api.arg("--shot", ""))
            Qt.quit()
        })
    }
    Component.onCompleted: {
        Api.root = root
        var size = Api.arg("--size", "").split("x")
        if (size.length === 2) {
            width = parseInt(size[0])
            height = parseInt(size[1])
        }
    }
}
