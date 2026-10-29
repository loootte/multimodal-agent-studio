//! MAS 桌面壳（13.A / #74）：启动 Python sidecar，窗口加载带令牌的本机页面，关窗停服务。
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    mas_desktop_lib::run();
}
