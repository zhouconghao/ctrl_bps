# This file is part of ctrl_bps.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This software is dual licensed under the GNU General Public License and also
# under a 3-clause BSD license. Recipients may choose which of these licenses
# to use; please see the files gpl-3.0.txt and/or bsd_license.txt,
# respectively.  If you choose the GPL option then the following text applies
# (but note that there is still no warranty even if you opt for BSD instead):
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Classes and functions used in reporting run status.
"""

__all__ = ["BaseRunReport", "DetailedRunReport", "SummaryRunReport", "ExitCodesReport"]

import abc
import logging

from astropy.table import Table

from .wms_service import WmsStates

_LOG = logging.getLogger(__name__)


class BaseRunReport(abc.ABC):
    """The base class representing a run report.

    Parameters
    ----------
    fields : `list` [ `tuple` [ `str`, `str`]]
        The list of column specification, fields, to include in the report.
        Each field has a name and a type.
    """

    def __init__(self, fields):
        self._table = Table(dtype=fields)
        self._msg = None

    def __eq__(self, other):
        if isinstance(other, BaseRunReport):
            return all(self._table == other._table)
        return False

    def __len__(self):
        """Return the number of runs in the report."""
        return len(self._table)

    def __str__(self):
        lines = list(self._table.pformat_all())
        return "\n".join(lines)

    @property
    def message(self):
        """Extra information a method need to pass to its caller (`str`)."""
        return self._msg

    def clear(self):
        """Remove all entries from the report."""
        self._msg = None
        self._table.remove_rows(slice(len(self)))

    def sort(self, columns, ascending=True):
        """Sort the report entries according to one or more keys.

        Parameters
        ----------
        columns : `str` | `list` [ `str` ]
            The column(s) to order the report by.
        ascending : `bool`, optional
            Sort report entries in ascending order, default.

        Raises
        ------
        AttributeError
            Raised if supplied with non-existent column(s).
        """
        if isinstance(columns, str):
            columns = [columns]
        unknown_keys = set(columns) - set(self._table.colnames)
        if unknown_keys:
            raise AttributeError(
                f"cannot sort the report entries: column(s) {', '.join(unknown_keys)} not found"
            )
        self._table.sort(keys=columns, reverse=not ascending)

    @classmethod
    def from_table(cls, table):
        """Create a report from a table.

        Parameters
        ----------
        table : `astropy.table.Table`
            Information about a run in a tabular form.

        Returns
        -------
        inst : `lsst.ctrl.bps.bps_reports.BaseRunReport`
            A report created based on the information in the provided table.
        """
        inst = cls(table.dtype.descr)
        inst._table = table.copy()
        return inst

    @abc.abstractmethod
    def add(self, run_report, use_global_id=False):
        """Add a single run info to the report.

        Parameters
        ----------
        run_report : `lsst.ctrl.bps.WmsRunReport`
            Information for single run.
        use_global_id : `bool`, optional
            If set, use global run id. Defaults to False which means that
            the local id will be used instead.

            Only applicable in the context of a WMS using distributed job
            queues (e.g., HTCondor).
        """


class SummaryRunReport(BaseRunReport):
    """A summary run report."""

    def add(self, run_report, use_global_id=False):
        # Docstring inherited from the base class.

        # Flag any running workflow that might need human attention.
        run_flag = " "
        if run_report.state == WmsStates.RUNNING:
            if run_report.job_state_counts.get(WmsStates.FAILED, 0):
                run_flag = "F"
            elif run_report.job_state_counts.get(WmsStates.DELETED, 0):
                run_flag = "D"
            elif run_report.job_state_counts.get(WmsStates.HELD, 0):
                run_flag = "H"

        # Estimate success rate.
        percent_succeeded = "UNK"
        _LOG.debug("total_number_jobs = %s", run_report.total_number_jobs)
        _LOG.debug("run_report.job_state_counts = %s", run_report.job_state_counts)
        if run_report.total_number_jobs:
            succeeded = run_report.job_state_counts.get(WmsStates.SUCCEEDED, 0)
            _LOG.debug("succeeded = %s", succeeded)
            percent_succeeded = f"{int(succeeded / run_report.total_number_jobs * 100)}"

        row = (
            run_flag,
            run_report.state.name,
            percent_succeeded,
            run_report.global_wms_id if use_global_id else run_report.wms_id,
            run_report.operator,
            run_report.project,
            run_report.campaign,
            run_report.payload,
            run_report.run,
        )
        self._table.add_row(row)


class DetailedRunReport(BaseRunReport):
    """A detailed run report."""

    def add(self, run_report, use_global_id=False):
        # Docstring inherited from the base class.

        # If run summary exists, use it to get the reference job counts.
        by_label_expected = {}
        if run_report.run_summary:
            for part in run_report.run_summary.split(";"):
                label, count = part.split(":")
                by_label_expected[label] = int(count)

        total = ["TOTAL"]
        total.extend([run_report.job_state_counts[state] for state in WmsStates])
        total.append(sum(by_label_expected.values()) if by_label_expected else run_report.total_number_jobs)
        self._table.add_row(total)

        # Use the provided job summary. If it doesn't exist, compile it from
        # information about individual jobs.
        if run_report.job_summary:
            job_summary = run_report.job_summary
        elif run_report.jobs:
            job_summary = compile_job_summary(run_report.jobs)
        else:
            id_ = run_report.global_wms_id if use_global_id else run_report.wms_id
            self._msg = f"WARNING: Job summary for run '{id_}' not available, report maybe incomplete."
            return

        if by_label_expected:
            job_order = list(by_label_expected)
        else:
            job_order = sorted(job_summary)
            self._msg = "WARNING: Could not determine order of pipeline, instead sorted alphabetically."
        for label in job_order:
            try:
                counts = job_summary[label]
            except KeyError:
                counts = dict.fromkeys(WmsStates, -1)
            else:
                if label in by_label_expected:
                    already_counted = sum(counts.values())
                    if already_counted != by_label_expected[label]:
                        counts[WmsStates.UNREADY] += by_label_expected[label] - already_counted

            run = [label]
            run.extend([counts[state] for state in WmsStates])
            run.append(by_label_expected[label] if by_label_expected else -1)
            self._table.add_row(run)

    def __str__(self):
        alignments = ["<"] + [">"] * (len(self._table.colnames) - 1)
        lines = list(self._table.pformat_all(align=alignments))
        lines.insert(3, lines[1])
        return str("\n".join(lines))


class ExitCodesReport(BaseRunReport):
    """An extension of run report to give information about
    error handling from the wms service.
    """

    def add(self, run_report, use_global_id=False):
        # Docstring inherited from the base class.

        # get labels from things and exit codes

        labels = []
        if run_report.run_summary:
            for part in run_report.run_summary.split(";"):
                label, _ = part.split(":")
                labels.append(label)
        else:
            id_ = run_report.global_wms_id if use_global_id else run_report.wms_id
            self._msg = f"WARNING: Job summary for run '{id_}' not available, report maybe incomplete."
            return
        exit_code_summary = run_report.exit_code_summary
        for label in labels:
            exit_codes = exit_code_summary[label]
            if exit_codes:
                # payload errors always return 1 on failure
                pipe_error_count = sum([code for code in exit_codes if code == 1])
                infra_codes = [code for code in exit_codes if code != 0 and code != 1]
                if infra_codes:
                    infra_error_count = len(infra_codes)
                    str_infra_codes = [str(code) for code in infra_codes]
                    infra_error_codes = ", ".join(sorted(set(str_infra_codes)))
                else:
                    infra_error_count = 0
                    infra_error_codes = "None"
            else:
                pipe_error_count = 0
                infra_error_codes = "None"
                infra_error_count = 0
            run = [label]
            run.extend([pipe_error_count, infra_error_count, infra_error_codes])
            self._table.add_row(run)

    def __str__(self):
        alignments = ["<"] + [">"] * (len(self._table.colnames) - 1)
        lines = list(self._table.pformat_all(align=alignments))
        return str("\n".join(lines))


def compile_job_summary(jobs):
    """Compile job summary from information available for individual jobs.

    Parameters
    ----------
    jobs : `list` [`lsst.ctrl.bps.WmsRunReport`]
        List of run reports.

    Returns
    -------
    job_summary : `dict` [`str`, dict` [`lsst.ctrl.bps.WmsState`, `int`]]
        The summary of the execution statuses for each job label in the run.
        For each job label, execution statuses are mapped to number of jobs
        having a given status.
    """
    job_summary = {}
    by_label = group_jobs_by_label(jobs)
    for label, job_group in by_label.items():
        by_label_state = group_jobs_by_state(job_group)
        _LOG.debug("by_label_state = %s", by_label_state)
        counts = {state: len(jobs) for state, jobs in by_label_state.items()}
        job_summary[label] = counts
    return job_summary


def group_jobs_by_state(jobs):
    """Divide given jobs into groups based on their state value.

    Parameters
    ----------
    jobs : `list` [`lsst.ctrl.bps.WmsJobReport`]
        Jobs to divide into groups based on state.

    Returns
    -------
    by_state : `dict`
        Mapping of job state to a list of jobs.
    """
    _LOG.debug("group_jobs_by_state: jobs=%s", jobs)
    by_state = {state: [] for state in WmsStates}
    for job in jobs:
        by_state[job.state].append(job)
    return by_state


def group_jobs_by_label(jobs):
    """Divide given jobs into groups based on their label value.

    Parameters
    ----------
    jobs : `list` [`lsst.ctrl.bps.WmsJobReport`]
        Jobs to divide into groups based on label.

    Returns
    -------
    by_label : `dict` [`str`, `lsst.ctrl.bps.WmsJobReport`]
        Mapping of job state to a list of jobs.
    """
    by_label = {}
    for job in jobs:
        group = by_label.setdefault(job.label, [])
        group.append(job)
    return by_label
