import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    width: 1220; height: 790
    minimumWidth: 850; minimumHeight: 620
    visible: true
    color: "#0b1119"
    title: "BlackWidow · Firmware emulator"
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
        anchors.margins: 28
        spacing: 16
        RowLayout {
            Layout.fillWidth: true
            ColumnLayout {
                spacing: 6
                Label { text: "BLACKWIDOW"; color: "#f0f6ff"; font.pixelSize: 27; font.weight: Font.DemiBold; font.letterSpacing: 3 }
                Label { text: "V4 75%   /   ORIGINAL FIRMWARE"; color: "#8195aa"; font.pixelSize: 11; font.letterSpacing: 2 }
            }
            Item { Layout.fillWidth: true }
            ActionButton { text: "Captures"; onClicked: capturesMenu.popup() }
            ActionButton { text: "Open firmware"; onClicked: firmwareDialog.open() }
            ActionButton { enabled: !emulator.usbipMode && (emulator.running || emulator.status === "Paused"); text: emulator.running ? "Pause" : "Resume"; onClicked: emulator.toggleRunning() }
            ActionButton { enabled: !emulator.usbipMode; text: "Reset"; onClicked: emulator.reset() }
        }
        Rectangle {
            Layout.fillWidth: true; Layout.fillHeight: true
            color: "#101a26"; radius: 18
            border.color: "#253344"
            Item {
                id: viewport
                anchors.fill: parent; anchors.margins: 10
                Item {
                    id: board
                    width: 750; height: 420
                    scale: Math.min(viewport.width / width, viewport.height / height)
                    anchors.centerIn: parent
                    Image { anchors.fill: parent; source: emulator.background; fillMode: Image.Stretch; smooth: true }
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
                            border.color: held ? "#ffffff" : (mouse.containsMouse ? "#90a8ba" : Qt.rgba(ledColor.r, ledColor.g, ledColor.b, 0.85))
                            Rectangle {
                                anchors.bottom: parent.bottom; anchors.bottomMargin: 3
                                anchors.horizontalCenter: parent.horizontalCenter
                                width: Math.min(parent.width - 8, 15); height: 2
                                radius: 1; color: keyItem.ledColor
                            }
                            ToolTip.visible: mouse.containsMouse
                            ToolTip.delay: 650
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
        }
        RowLayout {
            spacing: 24
            Rectangle { width: 7; height: 7; radius: 4; color: emulator.running ? "#8fe388" : "#8195aa" }
            Label { text: emulator.status; color: "#d0deea"; Layout.fillWidth: true; elide: Text.ElideRight }
            Label { text: "VIRTUAL TIME  " + emulator.virtualTime; color: "#8195aa"; font.family: "monospace" }
        }
        Label { text: emulator.captureSummary; color: "#8195aa"; font.pixelSize: 11 }
        Label { text: "Click to press · Live RGB is shown by key indicators · Photo lighting is decorative"; color: "#8195aa"; font.pixelSize: 12 }
        Rectangle {
            Layout.fillWidth: true; height: 44
            color: "#101a26"; radius: 8
            Label { anchors.centerIn: parent; text: "USB INPUT   " + emulator.report; color: "#a8bcd0"; font.family: "monospace"; font.pixelSize: 11 }
        }
    }
}
