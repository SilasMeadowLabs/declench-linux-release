desktop_conf = configuration_data()
desktop_conf.set('APP_ID', application_id)

desktop_file = configure_file(
  input: 'app.declench.linux.desktop.in',
  output: 'app.declench.linux.desktop',
  configuration: desktop_conf,
  install: true,
  install_dir: join_paths(get_option('datadir'), 'applications'),
)

desktop_utils = find_program('desktop-file-validate', required: false)
if desktop_utils.found()
  test('Validate desktop file', desktop_utils, args: [desktop_file])
endif

appstream_file = configure_file(
  input: 'app.declench.linux.metainfo.xml.in',
  output: 'app.declench.linux.metainfo.xml',
  configuration: desktop_conf,
  install: true,
  install_dir: join_paths(get_option('datadir'), 'metainfo'),
)

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test(
    'Validate appstream file',
    appstreamcli,
    args: ['validate', '--no-net', '--explain', appstream_file],
  )
endif

install_data(
  'app.declench.linux.gschema.xml',
  install_dir: join_paths(get_option('datadir'), 'glib-2.0', 'schemas'),
)

install_subdir(
  'icons/hicolor',
  install_dir: join_paths(get_option('datadir'), 'icons'),
)

