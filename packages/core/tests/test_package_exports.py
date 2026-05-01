def test_package_exports_context_window_and_runtime_settings_modules():
    import lumen_core
    import lumen_core.context_window as context_window
    import lumen_core.runtime_settings as runtime_settings

    assert lumen_core.context_window is context_window
    assert lumen_core.runtime_settings is runtime_settings
