import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    width: 1220; height: 790
    minimumWidth: 850; minimumHeight: 620
    visible: true
    title: "BlackWidow Firmware Emulator"
    onActiveChanged: if (!active) emulator.releaseAll()
    property url pendingBootloader: ""

    FileDialog {
        id: applicationDialog
        title: "Open application image"
        onAccepted: emulator.loadApplication(selectedFile, window.pendingBootloader)
    }
    FileDialog {
        id: bootloaderDialog
        title: "Open raw bootloader image"
        onAccepted: {
            window.pendingBootloader = selectedFile
            applicationDialog.open()
        }
    }
    FileDialog {
        id: fullImageDialog
        title: "Open complete flash image"
        onAccepted: emulator.loadFullImage(selectedFile)
    }
    Menu {
        id: loadMenu
        MenuItem {
            text: "Application only…"
            onTriggered: {
                window.pendingBootloader = ""
                applicationDialog.open()
            }
        }
        MenuItem { text: "Bootloader and application…"; onTriggered: bootloaderDialog.open() }
        MenuItem { text: "Complete flash image…"; onTriggered: fullImageDialog.open() }
    }
    Dialog {
        id: usbipDialog
        title: "USB/IP"
        anchors.centerIn: parent
        modal: true
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAboutToShow: {
            exportKeyboard.checked = emulator.usbipMode
            exportPort.value = emulator.usbipPort || 3240
        }
        onAccepted: emulator.configureUsbip(exportKeyboard.checked ? exportPort.value : 0)

        ColumnLayout {
            CheckBox { id: exportKeyboard; text: "Export keyboard over USB/IP" }
            RowLayout {
                enabled: exportKeyboard.checked
                Label { text: "Loopback port" }
                SpinBox { id: exportPort; from: 1; to: 65535; editable: true }
            }
            Label { text: "Changing these settings restarts loaded firmware." }
        }
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            CheckBox {
                text: "Limit to 1×"
                checked: emulator.limitSpeed
                onToggled: emulator.setLimitSpeed(checked)
            }
            Button { text: "USB/IP…"; onClicked: usbipDialog.open() }
            Button { text: "Load firmware"; onClicked: loadMenu.popup() }
            Button { enabled: !emulator.usbipMode && (emulator.running || emulator.status === "Paused"); text: emulator.running ? "Pause" : "Resume"; onClicked: emulator.toggleRunning() }
            Button {
                text: "Reset"
                onClicked: emulator.reset()
                ToolTip.visible: hovered && emulator.usbipMode
                ToolTip.text: "Restart firmware and disconnect any USB/IP guest"
            }
        }
        Item {
            id: viewport
            Layout.fillWidth: true
            Layout.fillHeight: true
            Item {
                id: board
                width: emulator.boardWidth; height: emulator.boardHeight
                scale: Math.min(viewport.width / width, viewport.height / height)
                anchors.centerIn: parent
                Image { anchors.fill: parent; source: "../assets/keyboard-unlit.png" }
                Column {
                    objectName: "volumeWheel"
                    x: emulator.wheelRegion.x; y: emulator.wheelRegion.y
                    width: emulator.wheelRegion.width; height: emulator.wheelRegion.height
                    enabled: emulator.running
                    Repeater {
                        model: [1, -1]
                        delegate: MouseArea {
                            required property int modelData
                            width: emulator.wheelRegion.width
                            height: emulator.wheelRegion.height / 2
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: emulator.turnWheel(modelData)
                            ToolTip.visible: containsMouse
                            ToolTip.text: modelData > 0 ? "Volume up" : "Volume down"
                        }
                    }
                }
                Repeater {
                    model: emulator.keys
                    delegate: Rectangle {
                        id: keyItem
                        objectName: "key_" + modelData.id
                        required property var modelData
                        required property int index
                        x: modelData.x; y: modelData.y
                        width: modelData.width; height: modelData.height
                        radius: Math.min(5, height / 2)
                        property color ledColor: emulator.colors[modelData.id] || "#000000"
                        property bool held: emulator.pressed.indexOf(index) !== -1
                        color: Qt.rgba(ledColor.r, ledColor.g, ledColor.b, 0.30)
                        border.width: held ? 2 : 0.8
                        border.color: held ? window.palette.highlight : (mouse.containsMouse ? window.palette.mid : Qt.rgba(ledColor.r, ledColor.g, ledColor.b, 0.85))
                        Rectangle {
                            anchors.bottom: parent.bottom; anchors.bottomMargin: 3
                            anchors.horizontalCenter: parent.horizontalCenter
                            width: Math.min(parent.width - 8, 15); height: 2
                            radius: 1; color: keyItem.ledColor
                        }
                        ToolTip.visible: mouse.containsMouse
                        ToolTip.text: modelData.label
                        MouseArea {
                            id: mouse
                            anchors.fill: parent
                            hoverEnabled: true
                            enabled: emulator.running
                            acceptedButtons: Qt.LeftButton | Qt.RightButton
                            onPressed: mouse => {
                                if (mouse.button === Qt.LeftButton)
                                    emulator.key(keyItem.index, true)
                            }
                            onReleased: mouse => {
                                if (mouse.button === Qt.LeftButton)
                                    emulator.key(keyItem.index, false)
                            }
                            onClicked: mouse => {
                                if (mouse.button === Qt.RightButton)
                                    emulator.toggleKey(keyItem.index)
                            }
                            onCanceled: emulator.key(keyItem.index, false)
                        }
                    }
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Label { text: emulator.status; Layout.fillWidth: true; elide: Text.ElideRight }
            Label { text: emulator.virtualTime + " · " + emulator.speed }
        }
        Label { text: emulator.captureSummary }
        Label {
            visible: emulator.usbipMode
            text: "USB/IP · 127.0.0.1:" + emulator.usbipPort + " · Bus ID 1-1"
        }
        Label { text: "USB input: " + emulator.report }
    }
}
