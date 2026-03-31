use tauri::{
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, Manager,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};
use tauri_plugin_notification::NotificationExt;

#[tauri::command]
fn send_notification(app: AppHandle, title: String, body: String) {
    let _ = app.notification().builder().title(&title).body(&body).show();
}

#[tauri::command]
fn set_tray_badge(app: AppHandle, count: u32) {
    if let Some(tray) = app.tray_by_id("main-tray") {
        let tooltip = if count > 0 {
            format!("aiventbus — {} pending", count)
        } else {
            "aiventbus".to_string()
        };
        let _ = tray.set_tooltip(Some(&tooltip));
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state == ShortcutState::Pressed {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                            let _ = window.emit("focus-chat", ());
                        }
                    }
                })
                .build(),
        )
        .setup(|app| {
            // Register Ctrl+Space global shortcut
            let shortcut = Shortcut::new(Some(Modifiers::CONTROL), Code::Space);
            if let Err(e) = app.global_shortcut().register(shortcut) {
                eprintln!("Failed to register global shortcut: {}", e);
            }

            // Build tray icon
            let _ = TrayIconBuilder::with_id("main-tray")
                .tooltip("aiventbus")
                .icon(app.default_window_icon().unwrap().clone())
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![send_notification, set_tray_badge])
        .run(tauri::generate_context!())
        .expect("error while running aiventbus widget");
}
