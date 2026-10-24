mod backend;

use tauri::RunEvent;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(backend::BackendState::default())
        .invoke_handler(tauri::generate_handler![
            backend::backend_info,
            backend::restart_backend,
            backend::save_text_file
        ])
        .setup(|app| {
            backend::start(app.handle());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building LocPilot");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            backend::stop(handle);
        }
    });
}
