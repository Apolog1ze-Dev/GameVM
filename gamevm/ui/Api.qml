// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Karim Dagher
pragma Singleton
import QtQuick

// The link to the local gamevm service (gamevm_ui.py): its snapshot `s`, polled, and `act()`.
QtObject {
    id: api
    property var s: ({})
    property bool ready: false
    property string base: ""
    property string token: ""
    property string error: ""
    property string toast: ""
    property string page: arg("--page", "home")
    property Item root: null          // the window's content, where Modal.qml puts its questions

    // Parts of the snapshot with safe defaults, so pages can bind before the first answer.
    readonly property var vm: s.vm || ({ state: "off", mode: "igpu", desktop: "", lookingGlass: false, toggle: "", name: "" })
    readonly property var busy: s.busy || ({ active: false, kind: "", title: "", lines: [] })
    readonly property var needs: s.needs || []
    readonly property var done: s.done || []
    readonly property var sharing: s.sharing || ({ state: "off", canSend: false, why: "", linked: false, device: "", link: true,
                                                   drives: [], transfers: [], downloads: "", winDownloads: "", winTool: false })
    readonly property var resources: s.resources || ({ cores: [], disks: [], gpus: [], usb: [], recommended: {} })
    readonly property var setup: s.setup || ({ steps: [], left: 0, yours: 0 })
    readonly property var settings: s.settings || ({ toggles: [] })
    readonly property var help: s.help || ({ doctor: { sections: [], fails: 0, warns: 0, when: "", running: false, ran: false }, log: [] })

    function arg(name, fallback) {
        var a = Qt.application.arguments, i = a.indexOf(name)
        return i >= 0 && i + 1 < a.length ? a[i + 1] : fallback
    }

    function request(method, path, body, done) {
        var x = new XMLHttpRequest()
        x.open(method, base + path)
        x.setRequestHeader("X-Gamevm-Token", token)
        if (body !== null)
            x.setRequestHeader("Content-Type", "application/json")
        x.onreadystatechange = function() {
            if (x.readyState !== XMLHttpRequest.DONE)
                return
            var o = null
            try { o = JSON.parse(x.responseText) } catch (e) { }
            if (x.status === 200 && o !== null) {
                error = ""
                done(o)
            } else {
                error = "The gamevm service does not answer. Close this window and open gamevm again."
            }
        }
        x.send(body === null ? undefined : JSON.stringify(body))
    }

    function refresh() {
        if (base === "")
            return
        request("GET", "/state", null, function(o) {
            if (o.rev !== s.rev)
                s = o
            ready = true
        })
    }

    // Ask the service to do something. `done(result)` gets { ok, message, data }.
    function act(id, args, done) {
        request("POST", "/action", { id: id, args: args || {} }, function(r) {
            if (r.message)
                toast = r.message
            if (r.data && r.data.page)
                page = r.data.page
            if (done)
                done(r)
            refresh()
        })
    }

    function connect() {
        var file = arg("--link", "")
        var x = new XMLHttpRequest()
        x.open("GET", "file://" + file)
        x.onreadystatechange = function() {
            if (x.readyState !== XMLHttpRequest.DONE)
                return
            try {
                var o = JSON.parse(x.responseText)
                token = o.token
                base = "http://127.0.0.1:" + o.port
                refresh()
            } catch (e) {
                error = "This window has to be opened with: gamevm ui"
            }
        }
        x.send()
    }

    property Timer poll: Timer {
        interval: 1500
        repeat: true
        running: api.base !== ""
        onTriggered: api.refresh()
    }

    Component.onCompleted: connect()
}
