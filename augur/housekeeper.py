#SPDX-License-Identifier: MIT
"""
Keeps data up to date
"""
import coloredlogs
from copy import deepcopy
import logging, os, time, requests
import logging.config
from multiprocessing import Process, get_start_method
from sqlalchemy.ext.automap import automap_base
import sqlalchemy as s
import pandas as pd
from sqlalchemy import MetaData

from augur.logging import AugurLogging
from urllib.parse import urlparse

import warnings
warnings.filterwarnings('ignore')

logger = logging.getLogger(__name__)

class Housekeeper:

    def __init__(self, broker, augur_app):
        logger.info("Booting housekeeper")

        self._processes = []
        self.augur_logging = augur_app.logging
        self.jobs = deepcopy(augur_app.config.get_value("Housekeeper", "jobs"))
        self.update_redirects = deepcopy(augur_app.config.get_value("Housekeeper", "update_redirects"))
        self.broker_host = augur_app.config.get_value("Server", "host")
        self.broker_port = augur_app.config.get_value("Server", "port")
        self.broker = broker

        self.db = augur_app.database
        self.helper_db = augur_app.operations_database

        helper_metadata = MetaData()
        helper_metadata.reflect(self.helper_db, only=['worker_job'])
        HelperBase = automap_base(metadata=helper_metadata)
        HelperBase.prepare()
        self.job_table = HelperBase.classes.worker_job.__table__

        repoUrlSQL = s.sql.text("""
            SELECT repo_git FROM repo
        """)
        rs = pd.read_sql(repoUrlSQL, self.db, params={})
        all_repos = rs['repo_git'].values.tolist()

        # If enabled, updates all redirects of repositories 
        # and organizations urls for configured repo_group_id
        self.update_url_redirects()

        # List of tasks that need periodic updates
        self.schedule_updates()

    def schedule_updates(self):
        """
        Starts update processes
        """
        self.prep_jobs()
        self.augur_logging.initialize_housekeeper_logging_listener()
        logger.info("Scheduling update processes")
        for job in self.jobs:
            process = Process(target=self.updater_process, name=job["model"], args=(self.broker_host, self.broker_port, self.broker, job, (self.augur_logging.housekeeper_job_config, self.augur_logging.get_config())))
            self._processes.append(process)
            process.start()


    @staticmethod
    def updater_process(broker_host, broker_port, broker, job, logging_config):
        """
        Controls a given plugin's update process

        """
        logging.config.dictConfig(logging_config[0])
        logger = logging.getLogger(f"augur.jobs.{job['model']}")
        coloredlogs.install(level=logging_config[1]["log_level"], logger=logger, fmt=logging_config[1]["format_string"])

        if logging_config[1]["quiet"]:
            logger.disabled

        if 'repo_group_id' in job:
            repo_group_id = job['repo_group_id']
            logger.info('Housekeeper spawned {} model updater process for repo group id {}'.format(job['model'], repo_group_id))
        else:
            repo_group_id = None
            logger.info('Housekeeper spawned {} model updater process for repo ids {}'.format(job['model'], job['repo_ids']))

        try:
            compatible_worker_found = False
            # Waiting for compatible worker
            while True:
                if not compatible_worker_found:
                    for worker in list(broker._getvalue().keys()):
                        if job['model'] in broker[worker]['models'] and job['given'] in broker[worker]['given']:
                            compatible_worker_found = True
                    time.sleep(10)
                    continue

                logger.info("Housekeeper recognized that the broker has a worker that " + 
                    "can handle the {} model... beginning to distribute maintained tasks".format(job['model']))
                while True:
                    logger.info('Housekeeper updating {} model with given {}...'.format(
                        job['model'], job['given'][0]))
                    
                    if job['given'][0] == 'git_url' or job['given'][0] == 'github_url':
                        for repo in job['repos']:
                            if job['given'][0] == 'github_url' and 'github.com' not in repo['repo_git']:
                                continue
                            given_key = 'git_url' if job['given'][0] == 'git_url' else 'github_url'
                            task = {
                                "job_type": job['job_type'] if 'job_type' in job else 'MAINTAIN', 
                                "models": [job['model']], 
                                "display_name": "{} model for url: {}".format(job['model'], repo['repo_git']),
                                "given": {}
                            }
                            task['given'][given_key] = repo['repo_git']
                            if "focused_task" in repo:
                                task["focused_task"] = repo['focused_task']
                            try:
                                requests.post('http://{}:{}/api/unstable/task'.format(
                                    broker_host,broker_port), json=task, timeout=10)
                            except Exception as e:
                                logger.error("Error encountered: {}".format(e))

                            logger.debug(task)

                            time.sleep(10)

                    elif job['given'][0] == 'repo_group':
                        time.sleep(120)
                        task = {
                                "job_type": job['job_type'] if 'job_type' in job else 'MAINTAIN', 
                                "models": [job['model']], 
                                "display_name": "{} model for repo group id: {}".format(job['model'], repo_group_id),
                                "given": {
                                    "repo_group": job['repos']
                                }
                            }
                        try:
                            time.sleep(120)
                            requests.post('http://{}:{}/api/unstable/task'.format(
                                broker_host,broker_port), json=task, timeout=10)
                            time.sleep(120)
                        except Exception as e:
                            logger.error("Error encountered: {}".format(e))

                    logger.info("Housekeeper finished sending {} tasks to the broker for it to distribute to your worker(s)".format(len(job['repos'])))
                    time.sleep(job['delay'])

        except KeyboardInterrupt as e:
            pass

    def join_updates(self):
        """
        Join to the update processes
        """
        for process in self._processes:
            logger.debug(f"Joining {process.name} update process")
            process.join()

    def shutdown_updates(self):
        """
        Ends all running update processes
        """
        for process in self._processes:
            # logger.debug(f"Terminating {process.name} update process")
            process.terminate()

    def prep_jobs(self):
        for index, job in enumerate(self.jobs, start=1):
            self.printProgressBar(index, len(self.jobs), 'Preparing housekeeper jobs:', 'Complete', 1, 50)
            if 'repo_group_id' in job or 'repo_ids' in job:
                # If RG id is 0 then it just means to query all repos
                where_and = 'AND' if job['model'] == 'issues' and 'repo_group_id' in job else 'WHERE'
                where_condition = '{} repo_group_id = {}'.format(where_and, job['repo_group_id']
                    ) if 'repo_group_id' in job and job['repo_group_id'] != 0 else '{} repo.repo_id IN ({})'.format(
                    where_and, ",".join(str(id) for id in job['repo_ids'])) if 'repo_ids' in job else ''
                repo_url_sql = s.sql.text("""
                    SELECT 
                        repo.repo_id,
                        repo.repo_git,
                        recent_info.pull_request_count,
                        collected_pr_count,
                        ( repo_info.pull_request_count - pr_count.collected_pr_count ) AS pull_requests_missing 
                    FROM
                        augur_data.repo
                        LEFT OUTER JOIN ( SELECT COUNT ( * ) AS collected_pr_count, repo_id FROM pull_requests GROUP BY repo_id ) pr_count ON pr_count.repo_id = repo.repo_id
                        LEFT OUTER JOIN ( SELECT repo_id, MAX(pull_request_count) as pull_request_count,  MAX ( data_collection_date ) AS last_collected FROM augur_data.repo_info 
                            GROUP BY repo_id ) recent_info ON recent_info.repo_id = pr_count.repo_id
                        LEFT OUTER JOIN repo_info ON recent_info.repo_id = repo_info.repo_id 
                        AND repo_info.data_collection_date = recent_info.last_collected 
                        and recent_info.pull_request_count >=1 
                        and recent_info.pull_request_count is not null
                        {}
                    GROUP BY
                        repo.repo_id,
                        recent_info.pull_request_count,
                        pr_count.collected_pr_count,
                        repo_info.pull_request_count
                    ORDER BY
                        pull_requests_missing DESC NULLS LAST;
                    """.format(where_condition)) if job['model'] == 'pull_requests' else s.sql.text("""
                        SELECT
                            * 
                        FROM
                            (
                                ( SELECT repo_git, repo.repo_id, issues_enabled, COUNT ( * ) AS meta_count 
                                FROM repo left outer join repo_info on repo.repo_id = repo_info.repo_id
                                --WHERE issues_enabled = 'true' 
                                GROUP BY repo.repo_id, issues_enabled 
                                ORDER BY repo.repo_id ) zz
                                LEFT OUTER JOIN (
                                SELECT repo.repo_id,
                                    repo.repo_name,
                                    b.issues_count,
                                    d.repo_id AS issue_repo_id,
                                    e.last_collected,
                                    COUNT ( * ) AS issues_collected_count,
                                    (
                                    b.issues_count - COUNT ( * )) AS issues_missing,
                                    ABS (
                                    CAST (( COUNT ( * )) AS DOUBLE PRECISION ) / CAST ( b.issues_count + 1 AS DOUBLE PRECISION )) AS ratio_abs,
                                    (
                                    CAST (( COUNT ( * )) AS DOUBLE PRECISION ) / CAST ( b.issues_count + 1 AS DOUBLE PRECISION )) AS ratio_issues 
                                FROM
                                    augur_data.repo left outer join  
                                    augur_data.pull_requests d on d.repo_id = repo.repo_id left outer join 
                                    augur_data.repo_info b on d.repo_id = b.repo_id left outer join
                                    ( SELECT repo_id, MAX ( data_collection_date ) AS last_collected FROM augur_data.repo_info GROUP BY repo_id ORDER BY repo_id ) e 
                                                                        on e.repo_id = d.repo_id and b.data_collection_date = e.last_collected
                                WHERE d.pull_request_id IS NULL
                                {}
                                GROUP BY
                                    repo.repo_id,
                                    d.repo_id,
                                    b.issues_count,
                                    e.last_collected 
                                ORDER BY ratio_abs 
                                ) yy ON zz.repo_id = yy.repo_id 
                            ) D
                        ORDER BY ratio_abs NULLS FIRST
                    """.format(where_condition)) if job['model'] == 'issues' and 'repo_group_id' in job else s.sql.text(""" 
                        SELECT repo_git, repo_id FROM repo {} ORDER BY repo_id ASC
                    """.format(where_condition)) if 'order' not in job else s.sql.text(""" 
                        SELECT repo_git, repo.repo_id, count(*) as commit_count 
                        FROM augur_data.repo left outer join augur_data.commits 
                            on repo.repo_id = commits.repo_id 
                        {}
                        group by repo.repo_id ORDER BY commit_count {}
                    """.format(where_condition, job['order']))
                
                reorganized_repos = pd.read_sql(repo_url_sql, self.db, params={})
                if len(reorganized_repos) == 0:
                    logger.warning("Trying to send tasks for repo group, but the repo group does not contain any repos: {}".format(repo_url_sql))
                    job['repos'] = []
                    continue

                if 'starting_repo_id' in job:
                    last_id = job['starting_repo_id']
                else:
                    repoIdSQL = s.sql.text("""
                            SELECT since_id_str FROM worker_job
                            WHERE job_model = '{}'
                        """.format(job['model']))

                    job_df = pd.read_sql(repoIdSQL, self.helper_db, params={})

                    # If there is no job tuple found, insert one
                    if len(job_df) == 0:
                        job_tuple = {
                            'job_model': job['model'],
                            'oauth_id': 0
                        }
                        result = self.helper_db.execute(self.job_table.insert().values(job_tuple))
                        logger.debug("No job tuple for {} model was found, so one was inserted into the job table: {}".format(job['model'], job_tuple))

                    # If a last id is not recorded, start from beginning of repos 
                    #   (first id is not necessarily 0)
                    try:
                        last_id = int(job_df.iloc[0]['since_id_str'])
                    except:
                        last_id = 0

                jobHistorySQL = s.sql.text("""
                        SELECT max(history_id) AS history_id, status FROM worker_history
                        GROUP BY status
                        LIMIT 1
                    """)

                history_df = pd.read_sql(jobHistorySQL, self.helper_db, params={})

                finishing_task = False
                if len(history_df.index) != 0:
                    if history_df.iloc[0]['status'] == 'Stopped':
                        self.history_id = int(history_df.iloc[0]['history_id'])
                        finishing_task = True

                # Rearrange repos so the one after the last one that 
                #   was completed will be ran first (if prioritized ordering is not available/enabled)
                if job['model'] not in ['issues', 'pull_requests']:
                    before_repos = reorganized_repos.loc[reorganized_repos['repo_id'].astype(int) < last_id]
                    after_repos = reorganized_repos.loc[reorganized_repos['repo_id'].astype(int) >= last_id]

                    reorganized_repos = after_repos.append(before_repos)

                if 'all_focused' in job:
                    reorganized_repos['focused_task'] = job['all_focused']

                reorganized_repos = reorganized_repos.to_dict('records')
            
                if finishing_task:
                    reorganized_repos[0]['focused_task'] = 1
                
                job['repos'] = reorganized_repos

            elif 'repo_id' in job:
                job['repo_group_id'] = None
                repoUrlSQL = s.sql.text("""
                    SELECT repo_git, repo_id FROM repo WHERE repo_id = {}
                """.format(job['repo_id']))

                rs = pd.read_sql(repoUrlSQL, self.db, params={})

                if 'all_focused' in job:
                    rs['focused_task'] = job['all_focused']

                rs = rs.to_dict('records')

                job['repos'] = rs
            # time.sleep(120)

    def update_url_redirects(self):
        if 'switch' in self.update_redirects and self.update_redirects['switch'] == 1 and 'repo_group_id' in self.update_redirects:
            repos_urls = self.get_repos_urls(self.update_redirects['repo_group_id'])
            if self.update_redirects['repo_group_id'] == 0:
                logger.info("Repo Group Set to Zero for URL Updates")
            else:
                logger.info("Repo Group ID Specified.")
            for url in repos_urls:
                url = self.trim_git_suffix(url)
                if url:
                    r = requests.get(url, timeout=(4.44, 7.01))
                    check_for_update = url != r.url
                    if check_for_update:
                        self.update_repo_url(url, r.url, self.update_redirects['repo_group_id'])

    def trim_git_suffix(self, url):
        if url.endswith('.git'):
            url = url.replace('.git', '')
        elif url.endswith('.github.io'):
            url = url.replace('.github.io', '')
        elif url.endswith('/.github'):
            url = ''
        return url

    def get_repos_urls(self, repo_group_id):
        if self.update_redirects['repo_group_id'] == 0:
            repos_sql = s.sql.text("""
                    SELECT repo_git FROM repo
                """)
            logger.info("repo_group_id is 0")
        else:
            repos_sql = s.sql.text("""
                    SELECT repo_git FROM repo
                    WHERE repo_group_id = ':repo_group_id'
                """)

        repos = pd.read_sql(repos_sql, self.db, params={'repo_group_id': repo_group_id})

        if len(repos) == 0:
            logger.info("Did not find any repositories stored in augur_database for repo_group_id {}\n".format(repo_group_id))

        return repos['repo_git']

    def update_repo_url(self, old_url, new_url, repo_group_id):
        trimmed_new_url = self.trim_git_suffix(new_url)
        if not trimmed_new_url:
            logger.info("New repo is named .github : {} ... skipping \n".format(new_url))
            return
        else:
            new_url = trimmed_new_url

        old_repo_path = Housekeeper.parseRepoName(old_url)
        old_repo_group_name = old_repo_path[0]
        new_repo_path = Housekeeper.parseRepoName(new_url)
        new_repo_group_name = new_repo_path[0]

        if old_repo_group_name != new_repo_group_name:
            # verifying the old repo group name is available in the database
            old_rg_name_sql = s.sql.text("""
                SELECT rg_name FROM repo_groups
                WHERE repo_group_id = ':repo_group_id'
            """)
            old_rg_name_from_DB = pd.read_sql(old_rg_name_sql, self.db, params={'repo_group_id': repo_group_id})
            if len(old_rg_name_from_DB['rg_name']) > 0 and old_repo_group_name != old_rg_name_from_DB['rg_name'][0]:
                logger.info("Incoming old repo group name doesn't match the DB record for repo_group_id {} . Incoming name: {} DB record: {} \n".format(repo_group_id, old_repo_group_name, old_rg_name_from_DB['rg_name'][0]))

            # checking if the new repo group name already exists and
            # inserting it in repo_groups if it doesn't
            rg_name_check_sql = s.sql.text("""
                    SELECT rg_name, repo_group_id FROM repo_groups
                    WHERE rg_name = :new_repo_group_name
                """)
            rg_name_check = pd.read_sql(rg_name_check_sql, self.db, params={'new_repo_group_name': new_repo_group_name})
            new_rg_name_already_exists = len(rg_name_check['rg_name']) > 0

            if new_rg_name_already_exists:
                new_repo_group_id = rg_name_check['repo_group_id'][0]
            else:
                insert_sql = s.sql.text("""
                        INSERT INTO repo_groups("rg_name", "rg_description", "rg_website", "rg_recache", "rg_last_modified", "rg_type", "tool_source", "tool_version", "data_source", "data_collection_date")
                        VALUES (:new_repo_group_name, '', '', 0, CURRENT_TIMESTAMP, 'Unknown', 'Loaded by user', '1.0', 'Git', CURRENT_TIMESTAMP) RETURNING repo_group_id;
                    """)
                new_repo_group_id = self.db.execute(insert_sql, new_repo_group_name=new_repo_group_name).fetchone()[0]
                logger.info("Inserted repo group {} with id {}\n".format(new_repo_group_name, new_repo_group_id))

            new_repo_group_id = '%s' % new_repo_group_id
            update_sql = s.sql.text("""
                    UPDATE repo SET repo_git = :new_url, repo_path = NULL, repo_name = NULL, repo_status = 'New', repo_group_id = :new_repo_group_id
                    WHERE repo_git = :old_url
                """)
            self.db.execute(update_sql, new_url=new_url, new_repo_group_id=new_repo_group_id, old_url=old_url)
            logger.info("Updated repo url from {} to {}\n".format(new_url, old_url))

        else:
            update_sql = s.sql.text("""
                UPDATE repo SET repo_git = :new_url, repo_path = NULL, repo_name = NULL, repo_status = 'New'
                WHERE repo_git = :old_url
            """)
            self.db.execute(update_sql, new_url=new_url, old_url=old_url)
            logger.info("Updated repo url from {} to {}\n".format(new_url, old_url))

    def parseRepoName(repo_url):
        path = urlparse(repo_url).path
        parts = path.split('/')
        return parts[1:]


    def printProgressBar(self, iteration, total, prefix = '', suffix = '', decimals = 1, length = 100, fill = '█', printEnd = "\r"):
        """
        Call in a loop to create terminal progress bar
        @params:
            iteration   - Required  : current iteration (Int)
            total       - Required  : total iterations (Int)
            prefix      - Optional  : prefix string (Str)
            suffix      - Optional  : suffix string (Str)
            decimals    - Optional  : positive number of decimals in percent complete (Int)
            length      - Optional  : character length of bar (Int)
            fill        - Optional  : bar fill character (Str)
            printEnd    - Optional  : end character (e.g. "\r", "\r\n") (Str)
        """

        percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
        filledLength = int(length * iteration // total)
        bar = fill * filledLength + '-' * (length - filledLength)
        print(f'\r{prefix} |{bar}| {percent}% {suffix}', end='\r')
        # Print New Line on Complete
        if iteration == total:
            print()
