import sys
import os
project = "TRILL"
html_title = 'TRILL'
author = "Zachary A. Martinez"
sys.path.insert(0, os.path.abspath('..'))
extensions = ['sphinxarg.ext', 'myst_parser', 'sphinx_rtd_dark_mode', 'sphinx_copybutton']
autodoc_mock_imports = ['pytorch_lightning', 'torch', 'pyg_lib']
default_dark_mode = True
