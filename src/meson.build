bin_conf = configuration_data()
bin_conf.set('PYTHON', python.full_path())
bin_conf.set('VERSION', meson.project_version())
bin_conf.set('pkgdatadir', pkgdatadir)
bin_conf.set('pythondir', pythondir)

configure_file(
  input: 'declench-linux.in',
  output: 'declench-linux',
  configuration: bin_conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

declench_sources = [
  'declench_gnome/__init__.py',
  'declench_gnome/__main__.py',
  'declench_gnome/app.py',
  'declench_gnome/chart_widget.py',
  'declench_gnome/cloud_sync.py',
  'declench_gnome/coaching.py',
  'declench_gnome/local_store.py',
  'declench_gnome/schedule_math.py',
  'declench_gnome/themes.py',
  'declench_gnome/window.py',
]

python.install_sources(
  declench_sources,
  subdir: 'declench_gnome',
)

