"""Series are a collection of test runs."""

import logging
import os
import pathlib
import time
import copy

from pavilion import utils
from pavilion import commands
from pavilion import arguments
from pavilion import test_config
from pavilion.test_config import resolver
from pavilion.status_file import STATES
from pavilion.test_run import TestRun, TestRunError, TestRunNotFoundError

from pavilion.output import dbg_print # delete this later


class TestSeriesError(RuntimeError):
    """An error in managing a series of tests."""


def union_dictionary(dict1, dict2):
    """Combines two dictionaries with nested lists."""

    new_dict = {}
    dict2_keys = list(dict2.keys())

    for key, list_value in dict1.items():
        new_dict[key] = list_value
        if key in dict2.keys():
            new_dict[key].extend(dict2[key])
            dict2_keys.remove(key)
            new_dict[key] = list(set(new_dict[key]))

    for key in dict2_keys:
        new_dict[key] = dict2[key]

    return new_dict


def test_obj_from_id(pav_cfg, test_ids):
    """Return the test object(s) associated with the id(s) provided.

    :param dict pav_cfg: Base pavilion configuration.
    :param Union(list,str) test_ids: One or more test IDs."
    :return tuple(list(test_obj),list(failed_ids)): tuple containing a list of
        test objects and a list of test IDs for which no test could be found.
    """

    test_obj_list = []
    test_failed_list = []

    if not isinstance(test_ids, list):
        test_ids = [test_ids]

    for test_id in test_ids:
        try:
            test = TestRun.load(pav_cfg, test_id)
            test_obj_list.append(test)
        except (TestRunError, TestRunNotFoundError):
            test_failed_list.append(test_id)

    return test_obj_list, test_failed_list


class SeriesManager:
    """Series Manger"""

    def __init__(self, pav_cfg, series_obj, series_cfg):
        # set everything up

        self.pav_cfg = pav_cfg
        self.series_obj = series_obj
        self.series_cfg = series_cfg

        self.series_section = self.series_cfg['series']

        self.dep_graph = {}  # { test_name: [tests it depends on] }
        self.make_dep_graph()

        universal_modes = self.series_cfg['modes']

        self.test_info = {}
        # set up configs for tests
        # { test_name : { 'config': <loaded config> }
        temp_resolver = resolver.TestConfigResolver(self.pav_cfg)
        for test_name, test_config in self.series_section.items():
            self.test_info[test_name] = {}
            test_modes = test_config['modes']
            all_modes = universal_modes + test_modes
            raw_configs = temp_resolver.load_raw_configs([test_name],
                                                         [],
                                                         all_modes)
            for raw_config in raw_configs:
                raw_config['only_if'] = union_dictionary(
                    raw_config['only_if'], test_config['only_if']
                )
                raw_config['not_if'] = union_dictionary(
                    raw_config['not_if'], test_config['not_if']
                )
            self.test_info[test_name]['configs'] = raw_configs

        # set up args for tests
        # { test_name: { 'args': args } }
        for test_name, test_config in self.series_section.items():
            # self.test_info[test_name] = {}
            test_modes = test_config['modes']
            all_modes = universal_modes + test_modes

            args_list = ['run', '--series-id={}'.format(self.series_obj.id)]
            for mode in all_modes:
                args_list.append('-m{}'.format(mode))
            args_list.append(test_name)

            self.test_info[test_name]['args'] = args_list

        # create doubly linked graph
        for test_name in self.dep_graph:
            prev_list = self.dep_graph[test_name]
            self.test_info[test_name]['prev'] = prev_list

            next_list = []
            for t_n in self.dep_graph:
                if test_name in self.dep_graph[t_n]:
                    next_list.append(t_n)
            self.test_info[test_name]['next'] = next_list

        dbg_print(self.test_info, '\n')

        # run tests in order
        self.all_tests = list(self.test_info.keys())
        self.started = []
        self.finished = []
        self.not_started = []
        # kick off tests that aren't waiting on any tests to complete
        for test_name in self.test_info:
            if not self.test_info[test_name]['prev']:
                self.run_test(test_name)
                self.not_started = list(set(self.all_tests) - set(self.started))

        while len(self.not_started) != 0:

            self.check_and_update()
            temp_waiting = copy.deepcopy(self.not_started)
            for test_name in temp_waiting:
                ready = all(wait in self.finished for wait in self.test_info[
                    test_name]['prev'])
                if ready:
                    if self.series_section[test_name]['depends_pass'] == 'True':
                        if self.all_tests_passed(
                                self.test_info[test_name]['prev']):
                            self.run_test(test_name)
                            self.not_started.remove(test_name)
                        else:
                            for config in self.test_info[test_name]['configs']:
                                del config['only_if']
                                del config['not_if']
                                skipped_test = TestRun(self.pav_cfg, config)
                                skipped_test.status.set(
                                    STATES.COMPLETE,
                                    "Skipping. Previous test did not PASS.")
                                skipped_test.set_run_complete()
                                self.series_obj.add_tests([skipped_test])
                            self.not_started.remove(test_name)
                            self.finished.append(test_name)
                    else:
                        self.run_test(test_name)
                        self.not_started.remove(test_name)

            time.sleep(1)

    def run_test(self, test_name):
        # basically copy what the run command is doing here

        run_cmd = commands.get_command('run')
        arg_parser = arguments.get_parser()
        args = arg_parser.parse_args(self.test_info[test_name]['args'])
        run_cmd.run(self.pav_cfg, args)
        self.test_info[test_name]['obj'] = run_cmd.last_tests
        self.started.append(test_name)

    # determines if test/s is/are done running
    def is_done(self, test_name):
        if 'obj' not in self.test_info[test_name].keys():
            return False

        test_obj_list = self.test_info[test_name]['obj']
        for test_obj in test_obj_list:
            if not (test_obj.path / 'RUN_COMPLETE').exists():
                return False
        return True

    def all_tests_passed(self, test_names):

        for test_name in test_names:
            if 'obj' not in self.test_info[test_name].keys():
                return False

            test_obj_list = self.test_info[test_name]['obj']
            for test_obj in test_obj_list:
                if test_obj.results['result'] != 'PASS':
                    return False

        return True

    def check_and_update(self):

        temp_started = copy.deepcopy(self.started)
        for test_name in temp_started:
            if self.is_done(test_name):
                self.started.remove(test_name)
                self.finished.append(test_name)

    def make_dep_graph(self):
        # has to be a graph of test sets
        for test_name, test_config in self.series_section.items():
            self.dep_graph[test_name] = test_config['depends_on']


class TestSeries:
    """Series are a collection of tests. Every time """

    LOGGER_FMT = 'series({})'

    def __init__(self, pav_cfg, tests=None, _id=None):
        """Initialize the series.

        :param pav_cfg: The pavilion configuration object.
        :param list tests: The list of test objects that belong to this series.
        :param int _id: The test id number. If this is given, it implies that
            we're regenerating this series from saved files.
        """

        self.pav_cfg = pav_cfg
        self.tests = {}

        if tests:
            self.tests = {test.id: test for test in tests}

        series_path = self.pav_cfg.working_dir/'series'

        # We're creating this series from scratch.
        if _id is None:
            # Get the series id and path.
            try:
                self._id, self.path = TestRun.create_id_dir(series_path)
            except (OSError, TimeoutError) as err:
                raise TestSeriesError(
                    "Could not get id or series directory in '{}': {}"
                    .format(series_path, err))

            if tests:
                # Create a soft link to the test directory of each test in the
                # series.
                for test in tests:
                    link_path = utils.make_id_path(self.path, test.id)

                    try:
                        link_path.symlink_to(test.path)
                    except OSError as err:
                        raise TestSeriesError(
                            "Could not link test '{}' in series at '{}': {}"
                            .format(test.path, link_path, err))

            self._save_series_id()

        else:
            self._id = _id
            self.path = utils.make_id_path(series_path, self._id)

        self._logger = logging.getLogger(self.LOGGER_FMT.format(self._id))

    @property
    def id(self):  # pylint: disable=invalid-name
        """Return the series id as a string, with an 's' in the front to
differentiate it from test ids."""

        return 's{}'.format(self._id)

    @classmethod
    def from_id(cls, pav_cfg, id_):
        """Load a series object from the given id, along with all of its
associated tests."""

        try:
            id_ = int(id_[1:])
        except TypeError as err:
            pass

        series_path = pav_cfg.working_dir/'series'
        series_path = utils.make_id_path(series_path, id_)

        if not series_path.exists():
            raise TestSeriesError("No such series found: '{}' at '{}'"
                                  .format(id_, series_path))

        logger = logging.getLogger(cls.LOGGER_FMT.format(id_))

        tests = []
        for path in os.listdir(str(series_path)):
            link_path = series_path/path
            if link_path.is_symlink() and link_path.is_dir():
                try:
                    test_id = int(link_path.name)
                except ValueError:
                    logger.info(
                        "Bad test id in series from dir '%s'",
                        link_path)
                    continue

                try:
                    tests.append(TestRun.load(pav_cfg, test_id=test_id))
                except TestRunError as err:
                    logger.info(
                        "Error loading test %s: %s",
                        test_id, err
                    )

            else:
                logger.info("Polluted series directory in series '%s'",
                            series_path)
                raise ValueError(link_path)

        return cls(pav_cfg, tests, _id=id_)

    def add_tests(self, test_objs):
        """
        Adds tests to existing series.
        :param test_objs: List of test objects
        :return: None
        """

        for test in test_objs:
            self.tests[test.id] = test

            # attempt to make symlink
            link_path = utils.make_id_path(self.path, test.id)

            try:
                link_path.symlink_to(test.path)
            except OSError as err:
                raise TestSeriesError(
                    "Could not link test '{}' in series at '{}': {}"
                    .format(test.path, link_path, err))

    def _save_series_id(self):
        """Save the series id to the user's .pavilion directory."""

        # Save the last series we created to the .pavilion directory
        # in the user's home dir. Pavilion commands can use this so the
        # user doesn't actually have to know the series_id of tests.

        last_series_fn = self.pav_cfg.working_dir/'users'
        last_series_fn /= '{}.series'.format(utils.get_login())
        try:
            with last_series_fn.open('w') as last_series_file:
                last_series_file.write(self.id)
        except (IOError, OSError):
            # It's ok if we can't write this file.
            self._logger.warning("Could not save series id to '%s'",
                                 last_series_fn)

    @classmethod
    def load_user_series_id(cls, pav_cfg):
        """Load the last series id used by the current user."""
        logger = logging.getLogger(cls.LOGGER_FMT.format('<unknown>'))

        last_series_fn = pav_cfg.working_dir/'users'
        last_series_fn /= '{}.series'.format(utils.get_login())

        if not last_series_fn.exists():
            return None
        try:
            with last_series_fn.open() as last_series_file:
                return last_series_file.read().strip()
        except (IOError, OSError) as err:
            logger.warning("Failed to read series id file '%s': %s",
                           last_series_fn, err)
            return None

    @property
    def timestamp(self):
        """Return the unix timestamp for this series, based on the last
modified date for the test directory."""
        # Leave it up to the caller to deal with time properly.
        return self.path.stat().st_mtime
