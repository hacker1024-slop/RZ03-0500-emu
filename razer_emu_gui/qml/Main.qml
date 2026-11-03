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
    FileDialog {
        id: firmwareDialog
        title: "Open application firmware"
        nameFilters: ["Firmware images (*.bin)", "All files (*)"]
        onAccepted: emulator.openFirmware(selectedFile)
    }
    FolderDialog { id: flashDialog; title: "Add a verified flash-region capture"; onAccepted: emulator.addFlashDump(selectedFolder) }
    FolderDialog { id: bootDialog; title: "Select complete bootloader capture"; onAccepted: emulator.setBootloader(selectedFolder) }
    Menu {
        id: capturesMenu
        MenuItem { text: "Add flash capture…"; onTriggered: flashDialog.open() }
        MenuItem { text: "Load bootloader capture…"; onTriggered: bootDialog.open() }
    }
    component ActionButton: Button {
        id: control
        implicitWidth: 116; implicitHeight: 36
        contentItem: Text { text: control.text; color: control.enabled ? "#d7e6f5" : "#586d80"; font.pixelSize: 12; horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter }
        background: Rectangle { color: control.hovered ? "#28405a" : "#1a2a3d"; radius: 7; border.color: "#344b61" }
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        RowLayout {
            Layout.fillWidth: true
            Item { Layout.fillWidth: true }
            Button { text: "Captures"; onClicked: capturesMenu.popup() }
            Button { text: "Open firmware"; onClicked: firmwareDialog.open() }
            Button { enabled: !emulator.usbipMode && (emulator.running || emulator.status === "Paused"); text: emulator.running ? "Pause" : "Resume"; onClicked: emulator.toggleRunning() }
            Button { enabled: !emulator.usbipMode; text: "Reset"; onClicked: emulator.reset() }
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
                            onPressed: emulator.key(keyItem.index, true)
                            onReleased: emulator.key(keyItem.index, false)
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
        Label { text: "USB input: " + emulator.report }
    }
}
