"""
Functionality for generating grade reports.
"""
from collections import OrderedDict
from datetime import datetime
from itertools import chain, izip_longest, izip
from lazy import lazy
import logging
from pytz import UTC
import re
from time import time

from instructor_analytics.basic import list_problem_responses
from instructor_analytics.csvs import format_dictlist
from certificates.models import CertificateWhitelist, certificate_info_for_user
from courseware.courses import get_course_by_id
from lms.djangoapps.grades.context import grading_context_for_course
from lms.djangoapps.grades.new.course_grade_factory import CourseGradeFactory
from lms.djangoapps.teams.models import CourseTeamMembership
from lms.djangoapps.verify_student.models import SoftwareSecurePhotoVerification
from openedx.core.djangoapps.course_groups.cohorts import get_cohort, is_course_cohorted
from student.models import CourseEnrollment
from xmodule.partitions.partitions_service import PartitionService
from xmodule.split_test_module import get_split_user_partitions

from .runner import TaskProgress
from .utils import upload_csv_to_report_store


TASK_LOG = logging.getLogger('edx.celery.task')


class CourseGradeReportContext(object):
    """
    Internal class that provides a common context to use for a single grade
    report.  When a report is parallelized across multiple processes,
    elements of this context are serialized and parsed across process
    boundaries.
    """
    def __init__(self, _xmodule_instance_args, _entry_id, course_id, _task_input, action_name):
        self.task_info_string = (
            u'Task: {task_id}, '
            u'InstructorTask ID: {entry_id}, '
            u'Course: {course_id}, '
            u'Input: {task_input}'
        ).format(
            task_id=_xmodule_instance_args.get('task_id') if _xmodule_instance_args is not None else None,
            entry_id=_entry_id,
            course_id=course_id,
            task_input=_task_input,
        )
        self.action_name = action_name
        self.course_id = course_id
        self.task_progress = TaskProgress(self.action_name, total=None, start_time=time())

    @lazy
    def course(self):
        return get_course_by_id(self.course_id)

    @lazy
    def course_experiments(self):
        return get_split_user_partitions(self.course.user_partitions)

    @lazy
    def teams_enabled(self):
        return self.course.teams_enabled

    @lazy
    def cohorts_enabled(self):
        return is_course_cohorted(self.course_id)

    @lazy
    def graded_assignments(self):
        """
        Returns an OrderedDict that maps an assignment type to a dict of
        subsection-headers and average-header.
        """
        grading_context = grading_context_for_course(self.course_id)
        graded_assignments_map = OrderedDict()
        for assignment_type_name, subsection_infos in grading_context['all_graded_subsections_by_type'].iteritems():
            graded_subsections_map = OrderedDict()
            for subsection_index, subsection_info in enumerate(subsection_infos, start=1):
                subsection = subsection_info['subsection_block']
                header_name = u"{assignment_type} {subsection_index}: {subsection_name}".format(
                    assignment_type=assignment_type_name,
                    subsection_index=subsection_index,
                    subsection_name=subsection.display_name,
                )
                graded_subsections_map[subsection.location] = header_name

            average_header = u"{assignment_type}".format(assignment_type=assignment_type_name)

            # Use separate subsection and average columns only if
            # there's more than one subsection.
            separate_subsection_avg_headers = len(subsection_infos) > 1
            if separate_subsection_avg_headers:
                average_header += u" (Avg)"

            graded_assignments_map[assignment_type_name] = {
                'subsection_headers': graded_subsections_map,
                'average_header': average_header,
                'separate_subsection_avg_headers': separate_subsection_avg_headers
            }
        return graded_assignments_map

    def update_status(self, message):
        """
        Updates the status on the celery task to the given message.
        Also logs the update.
        """
        TASK_LOG.info(u'%s, Task type: %s, %s', self.task_info_string, self.action_name, message)
        return self.task_progress.update_task_state(extra_meta={'step': message})


class CourseGradeReport(object):
    """
    Class to encapsulate functionality related to generating Grade Reports.
    """
    @classmethod
    def generate(cls, _xmodule_instance_args, _entry_id, course_id, _task_input, action_name):
        """
        Public method to generate a grade report.
        """
        context = CourseGradeReportContext(_xmodule_instance_args, _entry_id, course_id, _task_input, action_name)
        return CourseGradeReport()._generate(context)

    def _generate(self, context):
        """
        Internal method for generating a grade report for the given context.
        """
        context.update_status(u'Starting grades')
        success_headers = self._success_headers(context)
        error_headers = self._error_headers()
        batched_rows = self._batched_rows(context)

        context.update_status(u'Compiling grades')
        success_rows, error_rows = self._compile(context, batched_rows)

        context.update_status(u'Uploading grades')
        self._upload(context, success_headers, success_rows, error_headers, error_rows)

        return context.update_status(u'Completed grades')

    def _success_headers(self, context):
        """
        Returns a list of all applicable column headers for this grade report.
        """
        return (
            ["Student ID", "Email", "Username", "Grade"] +
            self._grades_header(context) +
            (['Cohort Name'] if context.cohorts_enabled else []) +
            [u'Experiment Group ({})'.format(partition.name) for partition in context.course_experiments] +
            (['Team Name'] if context.teams_enabled else []) +
            ['Enrollment Track', 'Verification Status'] +
            ['Certificate Eligible', 'Certificate Delivered', 'Certificate Type']
        )

    def _error_headers(self):
        """
        Returns a list of error headers for this grade report.
        """
        return ["Student ID", "Username", "Error"]

    def _batched_rows(self, context):
        """
        A generator of batches of (success_rows, error_rows) for this report.
        """
        for users in self._batch_users(context):
            yield self._rows_for_users(context, users)

    def _compile(self, context, batched_rows):
        """
        Compiles and returns the complete list of (success_rows, error_rows) for
        the given batched_rows and context.
        """
        # partition and chain successes and errors
        success_rows, error_rows = izip(*batched_rows)
        success_rows = list(chain(*success_rows))
        error_rows = list(chain(*error_rows))

        # update metrics on task status
        context.task_progress.succeeded = len(success_rows)
        context.task_progress.failed = len(error_rows)
        context.task_progress.attempted = context.task_progress.succeeded + context.task_progress.failed
        context.task_progress.total = context.task_progress.attempted
        return success_rows, error_rows

    def _upload(self, context, success_headers, success_rows, error_headers, error_rows):
        """
        Creates and uploads a CSV for the given headers and rows.
        """
        date = datetime.now(UTC)
        upload_csv_to_report_store([success_headers] + success_rows, 'grade_report', context.course_id, date)
        if len(error_rows) > 0:
            error_rows = [error_headers] + error_rows
            upload_csv_to_report_store(error_rows, 'grade_report_err', context.course_id, date)

    def _grades_header(self, context):
        """
        Returns the applicable grades-related headers for this report.
        """
        graded_assignments = context.graded_assignments
        grades_header = []
        for assignment_info in graded_assignments.itervalues():
            if assignment_info['separate_subsection_avg_headers']:
                grades_header.extend(assignment_info['subsection_headers'].itervalues())
            grades_header.append(assignment_info['average_header'])
        return grades_header

    def _batch_users(self, context):
        """
        Returns a generator of batches of users.
        """
        def grouper(iterable, chunk_size=1, fillvalue=None):
            args = [iter(iterable)] * chunk_size
            return izip_longest(*args, fillvalue=fillvalue)
        users = CourseEnrollment.objects.users_enrolled_in(context.course_id)
        return grouper(users)

    def _user_grade_results(self, course_grade, context):
        """
        Returns a list of grade results for the given course_grade corresponding
        to the headers for this report.
        """
        grade_results = []
        for assignment_type, assignment_info in context.graded_assignments.iteritems():
            for subsection_location in assignment_info['subsection_headers']:
                try:
                    subsection_grade = course_grade.graded_subsections_by_format[assignment_type][subsection_location]
                except KeyError:
                    grade_result = u'Not Available'
                else:
                    if subsection_grade.graded_total.first_attempted is not None:
                        grade_result = subsection_grade.graded_total.earned / subsection_grade.graded_total.possible
                    else:
                        grade_result = u'Not Attempted'
                grade_results.append([grade_result])
            if assignment_info['separate_subsection_avg_headers']:
                assignment_average = course_grade.grader_result['grade_breakdown'].get(assignment_type, {}).get(
                    'percent'
                )
                grade_results.append([assignment_average])
        return [course_grade.percent] + list(chain.from_iterable(grade_results))

    def _user_cohort_group_names(self, user, context):
        """
        Returns a list of names of cohort groups in which the given user
        belongs.
        """
        cohort_group_names = []
        if context.cohorts_enabled:
            group = get_cohort(user, context.course_id, assign=False)
            cohort_group_names.append(group.name if group else '')
        return cohort_group_names

    def _user_experiment_group_names(self, user, context):
        """
        Returns a list of names of course experiments in which the given user
        belongs.
        """
        experiment_group_names = []
        for partition in context.course_experiments:
            group = PartitionService(context.course_id).get_group(user, partition, assign=False)
            experiment_group_names.append(group.name if group else '')
        return experiment_group_names

    def _user_team_names(self, user, context):
        """
        Returns a list of names of teams in which the given user belongs.
        """
        team_names = []
        if context.teams_enabled:
            try:
                membership = CourseTeamMembership.objects.get(user=user, team__course_id=context.course_id)
                team_names.append(membership.team.name)
            except CourseTeamMembership.DoesNotExist:
                team_names.append('')
        return team_names

    def _user_verification_mode(self, user, context):
        """
        Returns a list of enrollment-mode and verification-status for the
        given user.
        """
        enrollment_mode = CourseEnrollment.enrollment_mode_for_user(user, context.course_id)[0]
        verification_status = SoftwareSecurePhotoVerification.verification_status_for_user(
            user,
            context.course_id,
            enrollment_mode
        )
        return [enrollment_mode, verification_status]

    def _user_certificate_info(self, user, context, course_grade, whitelisted_user_ids):
        """
        Returns the course certification information for the given user.
        """
        certificate_info = certificate_info_for_user(
            user,
            context.course_id,
            course_grade.letter_grade,
            user.id in whitelisted_user_ids
        )
        TASK_LOG.info(
            u'Student certificate eligibility: %s '
            u'(user=%s, course_id=%s, grade_percent=%s letter_grade=%s gradecutoffs=%s, allow_certificate=%s, '
            u'is_whitelisted=%s)',
            certificate_info[0],
            user,
            context.course_id,
            course_grade.percent,
            course_grade.letter_grade,
            context.course.grade_cutoffs,
            user.profile.allow_certificate,
            user.id in whitelisted_user_ids,
        )
        return certificate_info

    def _rows_for_users(self, context, users):
        """
        Returns a list of rows for the given users for this report.
        """
        certificate_whitelist = CertificateWhitelist.objects.filter(course_id=context.course_id, whitelist=True)
        whitelisted_user_ids = [entry.user_id for entry in certificate_whitelist]
        success_rows, error_rows = [], []
        for user, course_grade, err_msg in CourseGradeFactory().iter(users, course_key=context.course_id):
            if not course_grade:
                # An empty gradeset means we failed to grade a student.
                error_rows.append([user.id, user.username, err_msg])
            else:
                success_rows.append(
                    [user.id, user.email, user.username] +
                    self._user_grade_results(course_grade, context) +
                    self._user_cohort_group_names(user, context) +
                    self._user_experiment_group_names(user, context) +
                    self._user_team_names(user, context) +
                    self._user_verification_mode(user, context) +
                    self._user_certificate_info(user, context, course_grade, whitelisted_user_ids)
                )
        return success_rows, error_rows


class ProblemGradeReport(object):
    @classmethod
    def generate(cls, _xmodule_instance_args, _entry_id, course_id, _task_input, action_name):
        """
        Generate a CSV containing all students' problem grades within a given
        `course_id`.
        """
        start_time = time()
        start_date = datetime.now(UTC)
        status_interval = 100
        enrolled_students = CourseEnrollment.objects.users_enrolled_in(course_id)
        task_progress = TaskProgress(action_name, enrolled_students.count(), start_time)

        # This struct encapsulates both the display names of each static item in the
        # header row as values as well as the django User field names of those items
        # as the keys.  It is structured in this way to keep the values related.
        header_row = OrderedDict([('id', 'Student ID'), ('email', 'Email'), ('username', 'Username')])

        graded_scorable_blocks = cls._graded_scorable_blocks_to_header(course_id)

        # Just generate the static fields for now.
        rows = [list(header_row.values()) + ['Grade'] + list(chain.from_iterable(graded_scorable_blocks.values()))]
        error_rows = [list(header_row.values()) + ['error_msg']]
        current_step = {'step': 'Calculating Grades'}

        course = get_course_by_id(course_id)
        for student, course_grade, err_msg in CourseGradeFactory().iter(enrolled_students, course):
            student_fields = [getattr(student, field_name) for field_name in header_row]
            task_progress.attempted += 1

            if not course_grade:
                # There was an error grading this student.
                if not err_msg:
                    err_msg = u'Unknown error'
                error_rows.append(student_fields + [err_msg])
                task_progress.failed += 1
                continue

            earned_possible_values = []
            for block_location in graded_scorable_blocks:
                try:
                    problem_score = course_grade.problem_scores[block_location]
                except KeyError:
                    earned_possible_values.append([u'Not Available', u'Not Available'])
                else:
                    if problem_score.first_attempted:
                        earned_possible_values.append([problem_score.earned, problem_score.possible])
                    else:
                        earned_possible_values.append([u'Not Attempted', problem_score.possible])

            rows.append(student_fields + [course_grade.percent] + list(chain.from_iterable(earned_possible_values)))

            task_progress.succeeded += 1
            if task_progress.attempted % status_interval == 0:
                task_progress.update_task_state(extra_meta=current_step)

        # Perform the upload if any students have been successfully graded
        if len(rows) > 1:
            upload_csv_to_report_store(rows, 'problem_grade_report', course_id, start_date)
        # If there are any error rows, write them out as well
        if len(error_rows) > 1:
            upload_csv_to_report_store(error_rows, 'problem_grade_report_err', course_id, start_date)

        return task_progress.update_task_state(extra_meta={'step': 'Uploading CSV'})

    @classmethod
    def _graded_scorable_blocks_to_header(cls, course_key):
        """
        Returns an OrderedDict that maps a scorable block's id to its
        headers in the final report.
        """
        scorable_blocks_map = OrderedDict()
        grading_context = grading_context_for_course(course_key)
        for assignment_type_name, subsection_infos in grading_context['all_graded_subsections_by_type'].iteritems():
            for subsection_index, subsection_info in enumerate(subsection_infos, start=1):
                for scorable_block in subsection_info['scored_descendants']:
                    header_name = (
                        u"{assignment_type} {subsection_index}: "
                        u"{subsection_name} - {scorable_block_name}"
                    ).format(
                        scorable_block_name=scorable_block.display_name,
                        assignment_type=assignment_type_name,
                        subsection_index=subsection_index,
                        subsection_name=subsection_info['subsection_block'].display_name,
                    )
                    scorable_blocks_map[scorable_block.location] = [header_name + " (Earned)",
                                                                    header_name + " (Possible)"]
        return scorable_blocks_map


class ProblemResponses(object):
    @classmethod
    def generate(cls, _xmodule_instance_args, _entry_id, course_id, task_input, action_name):
        """
        For a given `course_id`, generate a CSV file containing
        all student answers to a given problem, and store using a `ReportStore`.
        """
        start_time = time()
        start_date = datetime.now(UTC)
        num_reports = 1
        task_progress = TaskProgress(action_name, num_reports, start_time)
        current_step = {'step': 'Calculating students answers to problem'}
        task_progress.update_task_state(extra_meta=current_step)

        # Compute result table and format it
        problem_location = task_input.get('problem_location')
        student_data = list_problem_responses(course_id, problem_location)
        features = ['username', 'state']
        header, rows = format_dictlist(student_data, features)

        task_progress.attempted = task_progress.succeeded = len(rows)
        task_progress.skipped = task_progress.total - task_progress.attempted

        rows.insert(0, header)

        current_step = {'step': 'Uploading CSV'}
        task_progress.update_task_state(extra_meta=current_step)

        # Perform the upload
        problem_location = re.sub(r'[:/]', '_', problem_location)
        csv_name = 'student_state_from_{}'.format(problem_location)
        upload_csv_to_report_store(rows, csv_name, course_id, start_date)

        return task_progress.update_task_state(extra_meta=current_step)
