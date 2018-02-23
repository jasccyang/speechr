#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sun Jan  7 21:35:55 2018

"""
import praw
from prawcore.exceptions import NotFound

import itertools
import os
import csv
import datetime
import pandas as pd
import numpy as np
import logging
import json

import comment_classifier
import BagOfWordsClassifier
import hate_subreddit_finder
import sql_loader

class Crawler:
    def __init__(self):
        self.logger = logging.getLogger('default')
        
        app_path = os.path.dirname(os.path.abspath(__file__))
        self.app_config = self.get_app_config(app_path)
        
        self.reddit = praw.Reddit(self.app_config['reddit_bot'])

        slurs = self.load_csv_resource_to_list('racial_slurs', app_path)        
        violent_words = self.load_csv_resource_to_list('violent_words', app_path)
        subreddits_to_scan = self.load_csv_resource_to_list('policing_subreddits', app_path)
        
        self.CC = comment_classifier.CommentClassifier(slurs, violent_words)
        self.BOWC = BagOfWordsClassifier.BagOfWordsClassifier()
        
        self.HRS = hate_subreddit_finder.HateSubredditFinder(self.reddit, subreddits_to_scan)
        self.number_of_hate_subs = self.app_config['crawler']['num_hate_subs']
        self.offset = datetime.timedelta(self.app_config['crawler']['time_delta_in_days'])
        
        self.DB = sql_loader.SQL_Loader(self.app_config)
        self.subreddit_last_scanned_dates = self.DB.pull_sub_log()
        
    def get_app_config(self, app_path):
        config_path = os.path.join(os.path.sep, app_path, 'config', 'app_config') + '.json'
        if os.path.exists(config_path):
            with open(config_path, 'rt') as f:
                config = json.load(f)
        self.logger.info('loaded config {}'.format(config))
        return config
                
    def load_csv_resource_to_list(self, file_name, app_path):
        slurs_file = os.path.join(os.path.sep, app_path, 'ref', file_name) + '.csv'
        slurs = []
        with open(slurs_file, 'r') as file:
            rdr = csv.reader(file)
            for row in rdr:
                slurs.append(row)
        slurs = list(itertools.chain.from_iterable(slurs))
        return slurs
                
    def collect(self):
        self.logger.info('collecting...')
        i = 0
        
        hate_subs = self.HRS.find_unique_hate_subreddits(self.number_of_hate_subs)
        self.logger.info('hate subs: ' + str(hate_subs))

        columns = ['comment_id', 'created_utc', 'permalink','subreddit', 'vote_score', 'body', 'time_analyzed']
        self.potential_hate_comments = pd.DataFrame(data=np.zeros((0,len(columns))), columns=columns)
        
        scores_cols = ['comment_id', 'score']
        self.keyword_scores = pd.DataFrame(data=np.zeros((0,len(scores_cols))), columns=scores_cols)
        self.bag_of_words_scores = pd.DataFrame(data=np.zeros((0,len(scores_cols))), columns=scores_cols)
        
        comment_count_per_scan_cols = ['subreddit', 'time_scanned', 'keyword_score', 'bow_score_score']
        self.comment_count_per_scan = pd.DataFrame(data=np.zeros((0,len(comment_count_per_scan_cols))), columns=comment_count_per_scan_cols)
        
        for hate_sub in hate_subs:
            keyword_count, bowscore_count = 0, 0
            self.logger.info('--------------------------')
            self.logger.info('scanning next sub: ' + hate_sub)
            self.logger.info('--------------------------')
                    
            subreddit = self.reddit.subreddit(hate_sub)

            try:    
                last_scan_time = self.get_last_scan(hate_sub) 
                self.logger.info('last scan time {} '.format(str(last_scan_time)))
                submissions = list(subreddit.new(limit=50))
                for sub in submissions:
                    if datetime.datetime.utcfromtimestamp(sub.created_utc) > last_scan_time - self.offset:
                        sub.comments.replace_more(limit=0)
                        comment_list = sub.comments.list()
                        self.logger.debug('.. found {} comments'.format(len(comment_list)))
                        self.process_comment_list(comment_list, i, hate_sub, columns, last_scan_time) 
            
                if self.potential_hate_comments.shape[0] > 0:
                    comment_count_DF = pd.DataFrame([[hate_sub, \
                                                      datetime.datetime.utcnow(),\
                                                      len(self.keyword_scores),\
                                                      len(self.bag_of_words_scores)]])
    
                    self.comment_count_per_scan = self.comment_count_per_scan.append(comment_count_DF, ignore_index = True)
                    self.logger.info('hate sub: ' + str(hate_sub) \
                                     + '; keyword_score: ' + str(len(self.keyword_scores))\
                                     + '; bow score: ' + str(len(self.bag_of_words_scores)))
                    
                    self.load_and_clear_subreddit_comments_scores()
                        
            except NotFound as ex:
                self.logger.info('Subreddit {} not found'.format(hate_sub))
            except Exception as e:
                self.logger.info('Error processing sub: {}, {}'.format(hate_sub, e))

    def process_comment_list(self,comment_list, i, hate_sub, columns, last_scan_time):                    
        for comment in comment_list:
            if datetime.datetime.utcfromtimestamp(comment.created_utc) < last_scan_time:
                self.logger.debug('skipping')
                continue
            
            i += 1
            if i % 100 == 0:
                self.logger.info('processing comment # ' + str(i))

            keyword_score = self.CC.analyze(comment.body)
            bow_score = self.BOWC.analyze(comment.body)
            self.logger.debug('comment {}, keyword {}, bow {}'.format(comment.body, keyword_score, bow_score))
            
            if keyword_score > 0 or bow_score > 0:            
                time = datetime.datetime.utcfromtimestamp( comment.created_utc )
                current_time = datetime.datetime.utcnow()
                temp_df = pd.DataFrame([[comment.id, \
                                         time, \
                                         comment.permalink, \
                                         hate_sub, \
                                         comment.score, \
                                         comment.body, \
                                         current_time ]], 
                                       columns=columns)
                
                temp_keyword = pd.DataFrame([[comment.id, keyword_score]])
                temp_bowc = pd.DataFrame([[comment.id, bow_score]])
                                
                self.potential_hate_comments = self.potential_hate_comments.append(temp_df, ignore_index=True)
                self.keyword_scores = self.keyword_scores.append(temp_keyword, ignore_index=True)
                self.bag_of_words_scores = self.bag_of_words_scores.append(temp_bowc, ignore_index=True)
                
                self.logger.info(comment.body)
                self.logger.info('keyword_score: ' + str(keyword_score))
                self.logger.info('bag of words score: ' + str(bow_score))
                    
    def load_and_clear_subreddit_comments_scores(self):            
        self.DB.load_df(self.potential_hate_comments, 'comments', 'append')
        self.potential_hate_comments = self.potential_hate_comments.iloc[0:0]
        
        self.DB.load_df(self.keyword_scores, 'keyword_scores', 'append')
        self.keyword_scores = self.keyword_scores.iloc[0:0]
        
        self.DB.load_df(self.bag_of_words_scores, 'bow_scores', 'append')
        self.bag_of_words_scores = self.bag_of_words_scores.iloc[0:0]
        
        self.DB.load_df(self.comment_count_per_scan, 'comment_count_per_subreddit','append')
        self.comment_count_per_scan = self.comment_count_per_scan.iloc[0:0]
        
    def run(self):
        self.collect()
        self.log_current_run()
        self.load_summary_to_db()

    def log_current_run(self):
        self.logger.info('recording results of run...')
        time_run = datetime.datetime.utcnow()
        
        scanned_hate_subs = self.HRS.find_unique_hate_subreddits(self.number_of_hate_subs)
        columns = ['time_ran_utc', 'subreddit']
        self.scanned_hate_subs = pd.DataFrame(data=np.zeros((0,len(columns))), columns=columns)
        
        for _ in scanned_hate_subs:
            temp_df = pd.DataFrame([[
                    time_run,
                    _]],
                    columns=columns)
            self.scanned_hate_subs = self.scanned_hate_subs.append(temp_df, ignore_index=True)

    def load_summary_to_db(self):
        hate_sub_reports = self.HRS.get_hate_sub_reports(-1)
        self.DB.load_df(hate_sub_reports, 'hate_sub_reports', 'append')                                
        self.DB.load_df(self.scanned_hate_subs, 'scanned_log', 'append')
           
    def get_last_scan(self, subreddit):
        if self.subreddit_last_scanned_dates == None:
            return datetime.datetime.min + self.offset
        
        if self.subreddit_last_scanned_dates.get(subreddit) == None:
            return datetime.datetime.min + self.offset
        else:
            return self.subreddit_last_scanned_dates.get(subreddit)
