project(
  'declench-linux',
  version: '1.0.6',
  meson_version: '>= 0.62.0',
  default_options: [
    'warning_level=2',
    'werror=false',
  ],
)

gnome = import('gnome')
py_mod = import('python')
python = py_mod.find_installation('python3', required: true)

application_id = 'app.declench.linux'
pkgdatadir = join_paths(get_option('prefix'), get_option('datadir'), meson.project_name())
pythondir = python.get_install_dir()

subdir('data')
subdir('src')

gnome.post_install(
  glib_compile_schemas: true,
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)

