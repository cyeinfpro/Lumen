# -*- coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules("tiktoken_ext")
datas = collect_data_files("tiktoken")
