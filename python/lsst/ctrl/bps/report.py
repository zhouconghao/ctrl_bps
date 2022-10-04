# This file is part of ctrl_bps.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
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

"""Supporting functions for reporting on runs submitted to a WMS.

Note: Expectations are that future reporting effort will revolve around LSST
oriented database tables.
"""

import abc
import logging

from astropy.table import Table
from lsst.utils import doImport

from . import WmsStates

_LOG = logging.getLogger(__name__)


class BaseRunReport(abc.ABC):
    """The base class representing a run report.

    Parameters
    ----------
    fields : `list` [ `tuple` [ `str`, `str`]]
        The list of column specification, fields, to include in the report.
        Each field has a name and a type.
    """

    message = None
    """Any extra information a method may want to pass to its caller (`str`).
    """

    def __init__(self, fields):
        self.table = Table(dtype=fields)

    def __eq__(self, other):
        if isinstance(other, BaseRunReport):
            return all(self.table == other.table)
        return False

    def __len__(self):
        """Number of runs in the report."""
        return len(self.table)

    def __str__(self):
        lines = list(self.table.pformat_all())
        return "\n".join(lines)

    def clear(self):
        """Remove all entries from the report."""
        self.message = None
        self.table.remove_rows(slice(len(self)))

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
        unknown_keys = set(columns) - set(self.table.colnames)
        if unknown_keys:
            raise AttributeError(
                f"cannot sort the report entries: column(s) {', '.join(unknown_keys)} not found"
            )
        self.table.sort(keys=columns, reverse=not ascending)

    @classmethod
    def from_table(cls, table):
        """Create a report from a table.

        Parameters
        ----------
        table : `astropy.table.Table`

        Returns
        -------
        inst : `lsst.ctrl.bps.report.BaseRunReport
        """
        inst = cls(table.dtype.descr)
        inst.table = table.copy()
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


class AbridgedRunReport(BaseRunReport):
    """An abridged run report."""

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
        self.table.add_row(row)


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
        self.table.add_row(total)

        # Use the provided job summary. If it doesn't exist, compile it from
        # information about individual jobs.
        if run_report.job_summary:
            job_summary = run_report.job_summary
        elif run_report.jobs:
            job_summary = compile_job_summary(run_report.jobs)
        else:
            id_ = run_report.global_wms_id if use_global_id else run_report.wms_id
            self.message = f"WARNING: Job summary for run '{id_}' not available, report maybe incomplete."
            return

        if by_label_expected:
            job_order = list(by_label_expected)
        else:
            job_order = sorted(job_summary)
            self.message = "WARNING: Could not determine order of pipeline, instead sorted alphabetically."
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
            self.table.add_row(run)

    def __str__(self):
        alignments = ["<"] + [">"] * (len(self.table.colnames) - 1)
        lines = list(self.table.pformat_all(align=alignments))
        lines.insert(3, lines[1])
        return str("\n".join(lines))


def report(wms_service, run_id, user, hist_days, pass_thru, is_global=False):
    """Print out summary of jobs submitted for execution.

    Parameters
    ----------
    wms_service : `str`
        Name of the class.
    run_id : `str`
        A run id the report will be restricted to.
    user : `str`
        A username the report will be restricted to.
    hist_days : int
        Number of days
    pass_thru : `str`
        A string to pass directly to the WMS service class.
    is_global : `bool`, optional
        If set, all available job queues will be queried for job information.
        Defaults to False which means that only a local job queue will be
        queried for information.

        Only applicable in the context of a WMS using distributed job queues
        (e.g., HTCondor).
    """
    wms_service_class = doImport(wms_service)
    wms_service = wms_service_class({})

    # If reporting on single run, increase history until better mechanism
    # for handling completed jobs is available.
    if run_id:
        hist_days = max(hist_days, 2)

    runs, message = wms_service.report(run_id, user, hist_days, pass_thru, is_global=is_global)

    run_brief = AbridgedRunReport(
        [
            ("X", "S"),
            ("STATE", "S"),
            ("%S", "S"),
            ("ID", "S"),
            ("OPERATOR", "S"),
            ("PROJECT", "S"),
            ("CAMPAIGN", "S"),
            ("PAYLOAD", "S"),
            ("RUN", "S"),
        ]
    )
    if run_id:
        fields = [(" ", "S")] + [(state.name, "i") for state in WmsStates] + [("EXPECTED", "i")]
        run_report = DetailedRunReport(fields)
        for run in runs:
            run_brief.add(run, use_global_id=is_global)
            run_report.add(run, use_global_id=is_global)
            if run_report.message:
                print(run_report.message)

            print(run_brief)
            print("\n")
            print(f"Path: {run.path}")
            print(f"Global job id: {run.global_wms_id}")
            print("\n")
            print(run_report)

            run_brief.clear()
            run_report.clear()
        if not runs and not message:
            print(
                f"No records found for job id '{run_id}'. "
                f"Hints: Double check id, retry with a larger --hist value (currently: {hist_days}), "
                f"and/or use --global to search all job queues."
            )
    else:
        for run in runs:
            run_brief.add(run, use_global_id=is_global)
        run_brief.sort("ID")
        print(run_brief)
    if message:
        print(message)
        print("\n")


def compile_job_summary(jobs):
    """Compile job summary from information available for individual jobs.

    Parameters
    ----------
    jobs : `list` [`lsst.ctrl.bps.WmsRunReport`]
        List of

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
