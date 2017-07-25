# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from __future__ import absolute_import

from datetime import datetime, timedelta
from pokemongo_bot.base_task import BaseTask
from pokemongo_bot.worker_result import WorkerResult
from pokemongo_bot import inventory
from pokemongo_bot.item_list import Item

class CatchLimiter(BaseTask):
    SUPPORTED_TASK_API_VERSION = 1

    def __init__(self, bot, config):
        super(CatchLimiter, self).__init__(bot, config)
        self.bot = bot
        self.config = config
        self.enabled = self.config.get("enabled",False)
        self.min_balls = self.config.get("min_balls",20)
        self.resume_balls = self.config.get("resume_balls",100)
        self.duration = self.config.get("duration",15)
        self.no_log_until = datetime.now()
        self.min_ultraball_to_keep = 0
        for subVal in self.bot.config.raw_tasks:
            if "type" in subVal:
                if subVal["type"] == "CatchPokemon":
                    self.min_ultraball_to_keep = subVal["config"]["min_ultraball_to_keep"]
                    
        if not hasattr(self.bot, "catch_resume_at"): self.bot.catch_resume_at = None

    def work(self):
        if not self.enabled:
            return WorkerResult.SUCCESS

        now = datetime.now()
        balls_on_hand = self.get_pokeball_count() - self.min_ultraball_to_keep
        
        # If resume time has passed, resume catching tasks
        if self.bot.catch_disabled and now >= self.bot.catch_resume_at:
            if balls_on_hand > self.min_balls:
                self.emit_event(
                    'catch_limit_off',
                    formatted="Resume time has passed and balls on hand ({}) exceeds threshold {}. Re-enabling catch tasks.".
                        format(balls_on_hand,self.min_balls)
                )
                self.bot.catch_disabled = False

        # If balls_on_hand is more than resume_balls, resume catch tasks, if not softbanned
        if self.bot.softban is False and self.bot.catch_disabled and balls_on_hand >= self.resume_balls:
            self.emit_event(
                'catch_limit_off',
                formatted="Resume time hasn't passed yet, but balls on hand ({}) exceeds threshold {}. Re-enabling catch tasks.".
                    format(balls_on_hand, self.resume_at_balls)
            )
            self.bot.catch_disabled = False

        # If balls_on_hand less than threshold, pause catching tasks for duration minutes
        if not self.bot.catch_disabled and balls_on_hand <= self.min_balls:
            self.bot.catch_resume_at = now + timedelta(minutes = self.duration)
            self.no_log_until = now + timedelta(minutes = 2)
            self.bot.catch_disabled = True
            self.emit_event(
                'catch_limit_on',
                formatted="Balls on hand ({}) has reached threshold {}. Disabling catch tasks until {} or balls on hand > threshold (whichever is later).".
                    format(balls_on_hand, self.min_balls, self.bot.catch_resume_at.strftime("%H:%M:%S"))
            )

        if self.bot.catch_disabled and self.no_log_until <= now:
            if now >= self.bot.catch_resume_at:
                self.logger.info("All catch tasks disabled until balls on hand (%s) > threshold." % balls_on_hand)
            else:
                self.logger.info("All catch tasks disabled until %s or balls on hand (%s) >= %s" % (self.bot.catch_resume_at.strftime("%H:%M:%S"), balls_on_hand, self.resume_balls))
            self.no_log_until = now + timedelta(minutes = 2)

        return WorkerResult.SUCCESS

    def get_pokeball_count(self):
        return sum([inventory.items().get(ball.value).count for ball in [Item.ITEM_POKE_BALL, Item.ITEM_GREAT_BALL, Item.ITEM_ULTRA_BALL]])
