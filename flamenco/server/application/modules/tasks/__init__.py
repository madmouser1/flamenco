import os
import logging
import json
from PIL import Image
from platform import system
from threading import Thread
from sqlalchemy import func
from sqlalchemy import or_
from decimal import Decimal
from zipfile import ZipFile

from flask import abort
from flask import jsonify
from flask import render_template
from flask import request
from flask import send_from_directory
from flask.ext.restful import Resource
from flask.ext.restful import reqparse

from application import app
from application import db

from datetime import datetime
from datetime import timedelta

from application.utils import http_rest_request
from application.utils import get_file_ext

from application.modules.tasks.model import Task
from application.modules.managers.model import Manager
from application.modules.jobs.model import Job
from application.modules.projects.model import Project
from application.modules.settings.model import Setting
from application.modules.jobs.model import JobManagers

from requests.exceptions import ConnectionError

from werkzeug.datastructures import FileStorage

parser = reqparse.RequestParser()
parser.add_argument('id', type=int)
parser.add_argument('status', type=str)
parser.add_argument('log', type=str)
parser.add_argument('time_cost', type=int)
parser.add_argument('activity', type=str)
parser.add_argument('taskfile', type=FileStorage, location='files')

tasks_list_parser = reqparse.RequestParser()
tasks_list_parser.add_argument('job_id', type=int)

class TaskApi(Resource):
    @staticmethod
    def create_task(job_id, task_type, task_settings, name, child_id, parser):
        # TODO attribution of the best manager

        manager = JobManagers.query.filter_by(job_id=job_id).first()
        job = Job.query.get(job_id)

        task = Task(job_id=job_id,
            name=name,
            type=task_type,
            settings=json.dumps(task_settings),
            status='ready',
            priority=job.priority,
            manager_id = manager.manager_id,
            log=None,
            time_cost=None,
            activity=None,
            child_id=child_id,
            parser=parser,
            )
        db.session.add(task)
        db.session.commit()
        return task.id

    @staticmethod
    def create_tasks(job):
        """Send the job to the Job Compiler"""
        #from application.job_compilers.simple_blender_render import job_compiler
        module_name = 'application.job_compilers.{0}'.format(job.type)
        job_compiler = None
        try:
            module_loader = __import__(module_name, globals(), locals(), ['job_compiler'], 0)
            job_compiler = module_loader.job_compiler
        except ImportError, e:
            print('Cant find module {0}: {1}'.format(module_name, e))
            return

        project = Project.query.filter_by(id = job.project_id).first()
        job_compiler.compile(job, project, TaskApi.create_task)

    @staticmethod
    def start_task(manager, task):
        """Execute a single task
        We pass manager and task as objects (and at the moment we use a bad
        way to get the additional job information - should be done with join)
        """

        params={'priority':task.priority,
            'type':task.type,
            'parser':task.parser,
            'task_id':task.id,
            'job_id':task.job_id,
            'settings':task.settings}


        task.status = 'running'
        task.manager_id = manager.id
        db.session.add(task)
        db.session.commit()

        r = http_rest_request(manager.host, '/tasks/file/{0}'.format(task.job_id), 'get')
        # print ('testing file')
        # print (r)
        if not r['file']:
            job = Job.query.get(task.job_id)
            serverstorage = app.config['SERVER_STORAGE']
            projectpath = os.path.join(serverstorage, str(job.project_id))
            jobpath = os.path.join(projectpath, str(job.id))
            zippath = os.path.join(jobpath, "jobfile_{0}.zip".format(job.id))
            try:
                jobfile = [('jobfile', ('jobfile.zip', open(zippath, 'rb'), 'application/zip'))]
            except IOError, e:
                logging.error(e)
            try:
                # requests.post(serverurl, files = render_file , data = job_properties)
                r = http_rest_request(manager.host, '/tasks', 'post', params, files=jobfile)
            except ConnectionError:
                print ("Connection Error: {0}".format(serverurl))

        else:
            r = http_rest_request(manager.host, '/tasks', 'post', params)

        if not u'status' in r:
            task.status = 'running'
            task.manager_id = manager.id
            db.session.add(task)
            db.session.commit()
        # TODO  get a reply from the worker (running, error, etc)


    @staticmethod
    def delete_task(task_id):
        # At the moment this function is not used anywhere
        try:
            task = Tasks.query.get(task_id)
        except Exception, e:
            print(e)
            return 'error'
        task.delete_instance()
        logging.info("Deleted task {0}".format(task_id))

    @staticmethod
    def delete_tasks(job_id):
        tasks = Task.query.filter_by(job_id=job_id)
        for t in tasks:
            db.session.delete(t)
            db.session.commit()
        logging.info("All tasks deleted for job {0}".format(job_id))

    @staticmethod
    def stop_task(task_ids):
        """Stop a list of tasks
        """
        logging.info("Stopping tasks {0}".format(task_ids))
        managers = {}
        for task_id in task_ids:
            task = Task.query.get(task_id)
            manager = Manager.query.filter_by(id = task.manager_id).first()
            if not manager.id in managers:
                managers[manager.id] = []
            if manager.has_virtual_workers == 0:
                managers[manager.id].append(task_id)

        for man in managers:
            params = {'tasks': managers[man]}
            try:
                delete_task = http_rest_request(
                    manager.host,
                    '/tasks',
                    'delete',
                    params=params)
            except:
                logging.info("Error deleting task from Manager")
                return
                pass
            task.status = 'ready'
            db.session.add(task)
            db.session.commit()
        #print "Task %d stopped" % task_id

    @staticmethod
    def stop_tasks(job_id):
        """We stop all the tasks for a specific job
        """
        tasks = Task.query.\
            filter_by(job_id = job_id).\
            filter_by(status = 'running').\
            all()

        # print tasks
        tasklist = []
        for t in tasks:
            # print t
            tasklist.append(t.id)
        TaskApi.stop_task(tasklist)

    @staticmethod
    def generate_thumbnails(job, start, end):
        # FIXME problem with PIL (string index out of range)
        #thumb_dir = RENDER_PATH + "/" + str(job.id)
        project = Project.query.get(job.project_id)
        thumbnail_dir = os.path.join(project.render_path_server, str(job.id), 'thumbnails')
        if not os.path.exists(thumbnail_dir):
            print '[Debug] ' + os.path.abspath(thumbnail_dir) + " does not exist"
            os.makedirs(thumbnail_dir)
        for i in range(start, end + 1):
            # TODO make generic extension
            #img_name = ("0" if i < 10 else "") + str(i) + get_file_ext(job.format)
            img_name = '{0:05d}'.format(i) + get_file_ext(job.format)
            #file_path = thumb_dir + "/" + str(i) + '.thumb'
            file_path = os.path.join(thumbnail_dir, '{0:05d}'.format(i), '.thumb')
            # We can't generate thumbnail from multilayer with pillow
            if job.format != "MULTILAYER":
                if os.path.exists(file_path):
                    os.remove(file_path)
                frame = os.path.abspath(
                    os.path.join(project.render_path_server, str(job.id), img_name))
                img = Image.open(frame)
                img.thumbnail((150, 150), Image.ANTIALIAS)
                thumbnail_path = os.path.join(thumbnail_dir, '{0:05d}'.format(i) + '.thumb')
                img.save(thumbnail_path, job.format)

    def post(self, task_id):
        args = parser.parse_args()
        task_id = args['id']
        status = args['status'].lower()
        log = args['log']
        time_cost = args['time_cost']
        activity = args['activity']
        task = Task.query.get(task_id)
        if task is None:
            return '', 404

        job = Job.query.get(task.job_id)
        if job is None:
            return '', 404

        if status=="running" and task.status!="running":
            return '', 403

        if job.status!="running" and status!="aborted":
            return '', 403

        serverstorage = app.config['SERVER_STORAGE']
        projectpath = os.path.join(serverstorage, str(job.project_id))

        try:
            os.mkdir(projectpath)
        except:
            pass

        if args['taskfile']:
            jobpath = os.path.join(projectpath, str(job.id))
            try:
                os.mkdir(jobpath)
            except:
                pass
            try:
                os.mkdir(os.path.join(jobpath, 'output'))
            except:
                pass
            taskfile = os.path.join(
                    app.config['TMP_FOLDER'], 'taskfileout_{0}_{1}.zip'.format(job.id, task_id))
            args['taskfile'].save( taskfile )

            zippath = os.path.join(jobpath, 'output')

            try:
                with ZipFile(taskfile, 'r') as jobzip:
                    jobzip.extractall(path=zippath)
            except:
                os.remove(zippath)
                return '', 404

            os.remove(taskfile)

        tasks_finished = Task.query\
            .filter_by(job_id=job.id, status='finished').count()
        tasks_failed = Task.query\
            .filter_by(job_id=job.id, status='failed').count()
        tasks_aborted = Task.query\
            .filter_by(job_id=job.id, status='aborted').count()
        tasks_count = job.tasks.count()

        tasks_status = {'count': tasks_count,
                        'finished': tasks_finished,
                        'failed': tasks_failed,
                        'aborted': tasks_aborted}

        job.tasks_status = json.dumps(tasks_status)
        db.session.add(job)

        status_old = task.status
        task.status = status
        task.log = log
        task.time_cost = time_cost
        task.activity = activity
        task.last_activity = datetime.now()
        db.session.add(task)
        db.session.commit()

        if status != status_old:
            # manager = Manager.query.get(task.manager_id)
            logging.info('Task {0} changed from {1} to {2}'.format(task_id, status_old, status))

            # Check if all tasks have been completed
            if all((lambda t : t.status in ['finished', 'failed'])(t) for t in Task.query.filter_by(job_id=job.id).all()):
                failed_tasks = Task.query.filter_by(job_id=job.id, status='failed').count()
                print ('[Debug] %d tasks failed before') % failed_tasks
                if failed_tasks > 0 or status == 'failed':
                    job.status = 'failed'
                else:
                    job.status = 'completed'
                db.session.add(job)
                db.session.commit()

        return '', 204

    def get(self, task_id):
        task = Task.query.get_or_404(task_id)
        task_info = {
            'id': task.id,
            'name': task.name,
            'status': task.status,
            'priority': task.priority,
            'type': task.type,
            'log': task.log,
            'activity': task.activity,
            'parser': task.parser
        }
        return jsonify(task_info)


class TaskListApi(Resource):
    @staticmethod
    def get_tasks_list(tasks):
        tasks_list = []
        for task in tasks:
            task_info = {
                'id': task.id,
                'name': task.name,
                'status': task.status,
                'priority': task.priority,
                'type': task.type,
                'log': task.log,
                'activity': task.activity,
                'parser': task.parser
            }
            tasks_list.append(task_info)
        return tasks_list

    def get(self):
        args = tasks_list_parser.parse_args()
        job_id = args['job_id']
        if job_id:
            tasks = Task.query.filter_by(job_id=job_id).all()
        else:
            tasks = Task.query.all()
        tasks_list = TaskListApi.get_tasks_list(tasks)
        return jsonify(tasks=tasks_list)


class TaskGeneratorApi(Resource):
    def get(self):
        """Upon request from a manager, picks the first task available and returns
        it in JSON format.
        """
        task_nolocked = None
        task = None
        tasks = {}
        percentage_done = 0

        manager_uuid = request.args.get('uuid', None)

        if manager_uuid:
            manager = Manager.query.filter_by(uuid=manager_uuid).one()
        else:
            ip_address = request.remote_addr
            manager = Manager.query.filter_by(ip_address=ip_address).first()

        if not manager:
            return '', 404

        ten_minutes = datetime.now() - timedelta(minutes=10)
        tasks = Task.query\
            .filter(Task.last_activity < ten_minutes, Task.status == 'running')\
            .all()
        for task in tasks:
            manager_count = Manager.query\
                .filter_by(id=task.manager_id, has_virtual_workers=0)\
                .count()
            if not manager_count > 0:
                continue
            task.status = 'ready'
            db.session.add(task)
            db.session.commit()

        # Get running Jobs
        running_jobs = Job.query\
            .filter_by(status='running')\
            .order_by(Job.priority.desc(), Job.id.asc())\
            .all()

        for job in running_jobs:
            # Temporarily commented logic for disabling potentially failing jobs
            # # Are the tasks failing?
            # failing_tasks = Task.query\
            #     .filter(Task.job_id == job.id, Task.status == 'failed')\
            #     .count()
            # # If True set status to failed

            # if failing_tasks > 3:
            #     job = Job.query.get(job.id)
            #     job.status = 'failed'
            #     db.session.add(job)
            #     db.session.commit()
            #     continue

            # Get job tasks (locking Task table)
            tasks = Task.query.filter(
                Task.job_id == job.id,
                or_(Task.status == 'ready',
                    Task.status == 'aborted'),
                Task.manager_id == manager.id).with_for_update().\
                order_by(Task.priority.desc(), Task.id.asc())
            task = None
            unfinished_parents = False
            for t in tasks:
                # All the parents are finished?
                for tt in tasks:
                    if tt.child_id == t.id and tt.status != 'finished':
                        unfinished_parents = True
                        break
                # If False skip this task
                if unfinished_parents:
                    continue
                task = t
                break
            # Task found? Then break
            if task:
                break

        if not task:
            # Unlocking Task table on ROLLBACK
            db.session.rollback()
            return '', 404

        task.status = "running"
        task.last_activity = datetime.now()
        db.session.commit()

        job = Job.query.get(task.job_id)

        frame_count = 1
        current_frame = 0
        percentage_done = Decimal(current_frame) / Decimal(frame_count) * Decimal(100)
        percentage_done = round(percentage_done, 1)
        task = {"id": task.id,
                "job_id": task.job_id,
                "name": task.name,
                "status": task.status,
                "type": task.type,
                "settings": json.loads(task.settings),
                "log": task.log,
                "activity": task.activity,
                "manager_id": task.manager_id,
                "priority": task.priority,
                "child_id": task.child_id,
                "parser": task.parser,
                "time_cost": task.time_cost,
                "project_id": job.project_id,
                "chunk_start": 0,
                "chunk_end": 0,
                "current_frame": 0,
                "status": task.status,
                "percentage_done": percentage_done}

        return jsonify(**task)


class TaskFileOutputApi(Resource):
    def get(self, task_id):
        """Given a task_id returns the output zip file
        """
        serverstorage = app.config['SERVER_STORAGE']
        task = Task.query.get(task_id)
        job = Job.query.get(task.job_id)
        projectpath = os.path.join(serverstorage, str(job.project_id))
        jobpath = os.path.join(projectpath, str(job.id), 'output')
        return send_from_directory(jobpath, 'taskfileout_{0}_{1}.zip'.format(job.id, task_id))
