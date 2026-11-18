// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Layouts
import "."

// What Windows gets: processor cores, memory, devices, network. The page edits a draft; Save
// writes it into the configuration (asks for the password), and it applies at the next start.
Page {
    id: page
    readonly property var r: Api.resources
    readonly property bool wide: width >= 980

    property bool dirty: false
    property var winCores: []
    property bool isolate: true
    property int mem: 16
    property bool hugepages: true
    property bool lookingGlass: true
    property var usbWin: []
    property string newUsb: "ask"
    property string network: "macvtap"
    property bool privateLink: true

    function load() {
        winCores = r.cores.filter(function(c) { return c.windows }).map(function(c) { return c.id })
        isolate = !!r.isolate
        mem = r.mem || 16
        hugepages = !!r.hugepages
        lookingGlass = !!r.lookingGlass
        usbWin = r.usb.filter(function(d) { return d.owner === "windows" }).map(function(d) { return d.id })
        newUsb = r.newUsb || "ask"
        network = r.network || "macvtap"
        privateLink = !!r.privateLink
        dirty = false
    }

    function recommended() {
        var rec = r.recommended
        winCores = rec.windowsCores || winCores
        mem = rec.mem || mem
        isolate = true
        hugepages = true
        network = rec.network || "macvtap"
        privateLink = true
        dirty = true
    }

    function toggleCore(id) {
        var at = winCores.indexOf(id), next = winCores.slice()
        if (at >= 0)
            next.splice(at, 1)
        else
            next.push(id)
        if (next.length === 0 || next.length === r.cores.length)
            return                                       // each side keeps at least one core
        next.sort(function(a, b) { return a - b })
        winCores = next
        dirty = true
    }

    function setUsb(id, toWindows) {
        var next = usbWin.filter(function(x) { return x !== id })
        if (toWindows)
            next.push(id)
        usbWin = next
        dirty = true
    }

    function save() {
        Api.act("save-resources", { windowsCores: winCores, isolate: isolate, mem: mem, hugepages: hugepages,
                                    lookingGlass: lookingGlass, usbWindows: usbWin, newUsb: newUsb, network: network,
                                    privateLink: privateLink }, function(res) { if (res.ok) dirty = false })
    }

    Connections {
        target: Api
        function onSChanged() { if (!page.dirty) page.load() }
    }
    Component.onCompleted: load()

    // ---------------------------------------------------------------- heading
    RowLayout {
        Layout.fillWidth: true
        spacing: 16
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 6
            H { Layout.fillWidth: true; size: 30; text: "What Windows gets" }
            T {
                Layout.fillWidth: true
                Layout.maximumWidth: 640
                color: Theme.text2
                text: Api.vm.state === "off"
                      ? "gamevm chose these for your hardware. Change anything you like; it applies the next time Windows starts."
                      : "Windows is running with the saved values. Changes apply the next time it starts."
            }
        }
        Btn { text: "Back to recommended"; onClicked: page.recommended(); Layout.alignment: Qt.AlignBottom }
        Btn {
            kind: "primary"
            text: "Save"
            enabled: page.dirty && !Api.busy.active
            onClicked: page.save()
            Layout.alignment: Qt.AlignBottom
        }
    }

    GridLayout {
        Layout.fillWidth: true
        columns: page.wide ? 2 : 1
        columnSpacing: 16
        rowSpacing: 16

        // ------------------------------------------------------------ processor
        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 480
            pad: 20
            H { text: "Processor" }
            T {
                Layout.fillWidth: true
                font.pixelSize: 14
                color: Theme.text2
                textFormat: Text.StyledText
                text: "<font color=\"" + Theme.text + "\"><b>" + page.winCores.length + " of " + page.r.cores.length
                      + " cores</b></font> for Windows, " + page.winCores.length * (page.r.cores.length ? page.r.cores[0].threads.length : 1)
                      + " threads. Linux keeps " + (page.r.cores.length - page.winCores.length) + " for the desktop."
            }
            GridLayout {
                Layout.fillWidth: true
                columns: Math.min(8, Math.max(1, page.r.cores.length))
                columnSpacing: 6
                rowSpacing: 6
                Repeater {
                    model: page.r.cores
                    delegate: Button {
                        id: core
                        required property var modelData
                        readonly property bool win: page.winCores.indexOf(modelData.id) >= 0
                        Layout.fillWidth: true
                        implicitHeight: 56
                        text: modelData.id
                        checkable: false
                        Accessible.name: "Core " + modelData.id + ": " + (win ? "Windows" : "Linux")
                        onClicked: page.toggleCore(modelData.id)
                        contentItem: Text {
                            text: core.text
                            font.family: Theme.font
                            font.pixelSize: 15
                            font.weight: Font.DemiBold
                            color: core.win ? "#7A3A00" : Theme.linText
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: 10
                            color: core.win ? Theme.winBg : Theme.linBg
                            border.width: core.visualFocus ? 3 : 2
                            border.color: core.win ? Theme.winLine : Theme.primary
                        }
                        HoverHandler { cursorShape: Qt.PointingHandCursor }
                    }
                }
            }
            Flow {
                Layout.fillWidth: true
                spacing: 16
                Row {
                    spacing: 6
                    Rectangle { width: 14; height: 14; radius: 3; color: Theme.winBg; border.width: 2; border.color: Theme.winLine; anchors.verticalCenter: parent.verticalCenter }
                    T { text: "Windows"; font.pixelSize: 13; color: Theme.text2 }
                }
                Row {
                    spacing: 6
                    Rectangle { width: 14; height: 14; radius: 3; color: Theme.linBg; border.width: 2; border.color: Theme.primary; anchors.verticalCenter: parent.verticalCenter }
                    T { text: "Linux"; font.pixelSize: 13; color: Theme.text2 }
                }
                T { text: "Select a core to move it."; font.pixelSize: 13; color: Theme.text2 }
            }
            Check {
                Layout.fillWidth: true
                text: "Keep Linux' programs off Windows' cores while it runs"
                checked: page.isolate
                onToggled: { page.isolate = checked; page.dirty = true }
            }
            Item { Layout.fillHeight: true }
        }

        // ------------------------------------------------------------ memory
        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 480
            pad: 20
            H { text: "Memory" }
            T {
                Layout.fillWidth: true
                font.pixelSize: 14
                color: Theme.text2
                textFormat: Text.StyledText
                text: "<font color=\"" + Theme.text + "\" size=\"5\"><b>" + page.mem + " GiB</b></font> of " + page.r.memTotal
                      + " GiB for Windows. Linux keeps " + Math.max(0, page.r.memTotal - page.mem) + " GiB."
            }
            Slider {
                id: slider
                Layout.fillWidth: true
                implicitHeight: 44
                from: page.r.memMin || 4
                to: page.r.memMax || 16
                stepSize: 1
                snapMode: Slider.SnapAlways
                value: page.mem
                Accessible.name: "Memory for Windows, in GiB"
                onMoved: { page.mem = Math.round(value); page.dirty = true }
                background: Rectangle {
                    x: slider.leftPadding
                    y: slider.topPadding + slider.availableHeight / 2 - height / 2
                    width: slider.availableWidth
                    height: 6
                    radius: 3
                    color: Theme.line
                    Rectangle { width: slider.visualPosition * parent.width; height: parent.height; radius: 3; color: Theme.primary }
                }
                handle: Rectangle {
                    x: slider.leftPadding + slider.visualPosition * (slider.availableWidth - width)
                    y: slider.topPadding + slider.availableHeight / 2 - height / 2
                    width: 24
                    height: 24
                    radius: 12
                    color: Theme.card
                    border.width: slider.visualFocus ? 3 : 2
                    border.color: Theme.primary
                }
            }
            RowLayout {
                Layout.fillWidth: true
                T { text: (page.r.memMin || 4) + " GiB"; font.pixelSize: 13; color: Theme.text2 }
                Item { Layout.fillWidth: true }
                T { text: (page.r.memMax || 16) + " GiB"; font.pixelSize: 13; color: Theme.text2 }
            }
            Check {
                Layout.fillWidth: true
                text: "Use large memory pages, a little faster. Falls back by itself when Linux cannot free them."
                checked: page.hugepages
                onToggled: { page.hugepages = checked; page.dirty = true }
            }
            Item { Layout.fillHeight: true }
        }

        // ------------------------------------------------------------ disks
        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 480
            pad: 20
            spacing: 0
            H { text: "Disks"; Layout.bottomMargin: 10 }
            Repeater {
                model: page.r.disks
                delegate: ColumnLayout {
                    id: disk
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 0
                    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: 10
                        Layout.bottomMargin: 10
                        spacing: 12
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            T { Layout.fillWidth: true; text: disk.modelData.name + ", " + disk.modelData.size; font.weight: Font.DemiBold }
                            T {
                                Layout.fillWidth: true
                                font.pixelSize: 13
                                color: Theme.text2
                                text: disk.modelData.role === "windows" ? "Your existing Windows starts from it, as it is."
                                    : disk.modelData.role === "shared" ? "The shared drive. With Windows while it runs, back with Linux afterwards."
                                    : "Linux lives here. Never handed over."
                            }
                        }
                        Pill {
                            text: disk.modelData.role === "windows" ? "Windows" : disk.modelData.role === "shared" ? "Shared" : "Linux only"
                            tone: disk.modelData.role === "windows" ? "win" : disk.modelData.role === "shared" ? "grey" : "linux"
                        }
                    }
                }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
            T {
                Layout.fillWidth: true
                Layout.topMargin: 10
                font.pixelSize: 13
                color: Theme.text2
                text: "Which disk is which was decided when gamevm was installed."
            }
            Item { Layout.fillHeight: true }
        }

        // ------------------------------------------------------------ graphics
        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 480
            pad: 20
            spacing: 0
            H { text: "Graphics and screens"; Layout.bottomMargin: 10 }
            Repeater {
                model: page.r.gpus
                delegate: ColumnLayout {
                    id: gpu
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 0
                    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: 10
                        Layout.bottomMargin: 10
                        spacing: 12
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            T {
                                Layout.fillWidth: true
                                font.weight: Font.DemiBold
                                text: gpu.modelData.name + (gpu.modelData.role === "windows" ? ", with the main monitor" : ", with the side monitor")
                            }
                            T {
                                Layout.fillWidth: true
                                font.pixelSize: 13
                                color: Theme.text2
                                text: gpu.modelData.role === "windows" ? "Handed over whole while Windows runs. Linux uses it the rest of the time."
                                                                       : "Keeps the Linux desktop on screen meanwhile."
                            }
                        }
                        Pill {
                            text: gpu.modelData.role === "windows" ? "Windows" : "Linux only"
                            tone: gpu.modelData.role === "windows" ? "win" : "linux"
                        }
                    }
                }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
            Check {
                Layout.fillWidth: true
                visible: Api.vm.mode === "igpu"
                text: "Also offer Windows in a window on the side monitor"
                checked: page.lookingGlass
                onToggled: { page.lookingGlass = checked; page.dirty = true }
            }
            Item { Layout.fillHeight: true }
        }

        // ------------------------------------------------------------ USB
        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 480
            pad: 20
            spacing: 0
            H { text: "USB devices"; Layout.bottomMargin: 10 }
            Repeater {
                model: page.r.usb
                delegate: ColumnLayout {
                    id: usb
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: 0
                    Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
                    RowLayout {
                        Layout.fillWidth: true
                        Layout.topMargin: 6
                        Layout.bottomMargin: 6
                        spacing: 12
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            T { Layout.fillWidth: true; text: usb.modelData.name; font.weight: Font.DemiBold }
                            T {
                                Layout.fillWidth: true
                                visible: usb.modelData.owner === "both"
                                font.pixelSize: 13
                                color: Theme.text2
                                text: Api.vm.toggle + " switch it between Linux and Windows."
                            }
                        }
                        Pill { visible: usb.modelData.owner === "both"; text: "Both sides"; tone: "grey"; Layout.topMargin: 10; Layout.bottomMargin: 10 }
                        Pick {
                            visible: usb.modelData.owner !== "both"
                            Accessible.name: usb.modelData.name
                            model: ["Stays on Linux", "Goes to Windows"]
                            currentIndex: page.usbWin.indexOf(usb.modelData.id) >= 0 ? 1 : 0
                            onActivated: function(i) { page.setUsb(usb.modelData.id, i === 1) }
                        }
                    }
                }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 6
                spacing: 12
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    T { Layout.fillWidth: true; text: "Devices plugged in later"; font.weight: Font.DemiBold }
                    T { Layout.fillWidth: true; font.pixelSize: 13; color: Theme.text2; text: "Controllers, sticks, anything new while Windows runs." }
                }
                Pick {
                    Accessible.name: "Devices plugged in later"
                    model: ["Ask me once", "Stay on Linux", "Go to Windows"]
                    currentIndex: ["ask", "linux", "windows"].indexOf(page.newUsb)
                    onActivated: function(i) { page.newUsb = ["ask", "linux", "windows"][i]; page.dirty = true }
                }
            }
            Item { Layout.fillHeight: true }
        }

        // ------------------------------------------------------------ network
        Card {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.preferredWidth: 480
            pad: 20
            spacing: 0
            H { text: "Network"; Layout.bottomMargin: 4 }
            T { text: "How Windows reaches the internet"; font.pixelSize: 13; color: Theme.text2; Layout.bottomMargin: 8 }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
            Choice {
                Layout.fillWidth: true
                title: "Its own address on your network"
                detail: "Best for online games and the Xbox app. Needs a network cable."
                checked: page.network === "macvtap"
                onClicked: { page.network = "macvtap"; page.dirty = true }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
            Choice {
                Layout.fillWidth: true
                title: "Through Linux"
                detail: "Works over Wi-Fi too."
                checked: page.network === "nat"
                onClicked: { page.network = "nat"; page.dirty = true }
            }
            Rectangle { Layout.fillWidth: true; implicitHeight: 1; color: Theme.line }
            Check {
                Layout.fillWidth: true
                text: "Private link between Linux and Windows, for the clipboard and files"
                checked: page.privateLink
                onToggled: { page.privateLink = checked; page.dirty = true }
            }
            Item { Layout.fillHeight: true }
        }
    }
}
