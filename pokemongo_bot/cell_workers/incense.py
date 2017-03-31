# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import math
import time

from .utils import format_time

from pokemongo_bot.base_task import BaseTask
from pokemongo_bot.constants import Constants
from pokemongo_bot.worker_result import WorkerResult
from pokemongo_bot.item_list import Item
from pokemongo_bot.cell_workers.pokemon_catch_worker import PokemonCatchWorker
from pokemongo_bot import inventory
from pgoapi.utilities import f2i

ORDINARY_INCENSE = 401

UNKNOWN = 0
SUCCESS = 1

INCENSE_ALREADY_ACTIVE = 2
NONE_IN_INVENTORY = 3
LOCATION_UNSET = 4

INCENSE_ENCOUNTER_AVAILABLE = 1
INCENSE_ENCOUNTER_NOT_AVAILABLE = 2

class Incense(BaseTask):
    SUPPORTED_TASK_API_VERSION = 1

    def __init__(self, bot, config):
        super(Incense, self).__init__(bot, config)

    def initialize(self):
        self.current_started = 0
        self.current_cooldown = 0
        self.current_incense = None
        self.enabled = self.config.get("enabled", False)
        self.cooldown = self.config.get("cooldown", 600)
        self.incense_count = inventory.items().get(ORDINARY_INCENSE).count

    def work(self):
        if not self.enabled:
            return WorkerResult.SUCCESS
        now = time.time()
        # If there is an active incense, check for mons
        if self._current_active_incense():
            self._check_encounter()
        elif now < self.current_cooldown:
            # Use on cooldown.
            return WorkerResult.SUCCESS
        elif self._have_applied_incense():
            self._check_encounter();
        else:
            incense_count = inventory.items().get(ORDINARY_INCENSE).count
            if incense_count > 0:
                self._apply_incense()
                return WorkerResult.SUCCESS
            else:
                self.current_cooldown = time.time() + self.cooldown
                self.emit_event(
                    'use_incense',
                    formatted="No incense left!",
                    data={
                        'type': 'Ordinary',
                        'incense_count': inventory.items().get(ORDINARY_INCENSE).count
                    }
                )

    def _check_encounter(self):
        response_dict = self.bot.api.get_incense_pokemon(
            player_latitude=f2i(self.bot.position[0]),
            player_longitude=f2i(self.bot.position[1])
        )
        encounter = response_dict.get('responses', {}).get('GET_INCENSE_POKEMON', {})
        result = encounter.get('result', 0)
        if result is INCENSE_ENCOUNTER_NOT_AVAILABLE:
            return WorkerResult.SUCCESS
        if result is INCENSE_ENCOUNTER_AVAILABLE:
            # encounter the pokemon and hand off to catcher
            self.emit_event(
                'incensed_pokemon_found',
                level='info',
                formatted='Incense attracted a pokemon at {encounter_location}',
                data=encounter
            )
            worker = PokemonCatchWorker(encounter, self.bot)
            return_value = worker.work()

            return return_value

    def _have_applied_incense(self):
        for applied_item in inventory.applied_items().all():
            self.logger.info("Active item: %s" % applied_item)
            if applied_item.expire_ms > 0:
                    mins = format_time(applied_item.expire_ms * 1000)
                    self.logger.info("Not applying incense, currently active: %s, %s minutes remaining", applied_item.item.name, mins)
                    return True
            else:
                    return False

    def _apply_incense(self):
        response_dict = self.bot.api.use_incense(
            incense_type=ORDINARY_INCENSE
        )
        result = response_dict.get('responses', {}).get('USE_INCENSE', {}).get('result', 0)
        if result is INCENSE_ALREADY_ACTIVE:
            self.emit_event(
                'use_incense',
                formatted="Incense already active, can't use incense now.",
                data={
                    'type': 'Ordinary',
                    'incense_count': inventory.items().get(ORDINARY_INCENSE).count
                }
            )
            self.current_incense = ORDINARY_INCENSE
            self.current_started = time.time()
            self.current_cooldown = time.time() + self.cooldown
            self._check_encounter()
        elif result is NONE_IN_INVENTORY:
            self.emit_event(
                'use_incense',
                formatted="No incense left!",
                data={
                    'type': 'Ordinary',
                    'incense_count': inventory.items().get(ORDINARY_INCENSE).count
                }
            )
            self.current_cooldown = time.time() + self.cooldown
        elif result is SUCCESS:
            self.emit_event(
                'use_incense',
                formatted="Using {type} incense. {incense_count} incense remaining",
                data={
                    'type': "Ordinary",
                    'incense_count': inventory.items().get(ORDINARY_INCENSE).count
                }
            )
            self.current_incense = ORDINARY_INCENSE
            self.current_started = time.time()
            self.current_cooldown = time.time() + self.cooldown

    def _current_active_incense(self):
        now = time.time()
        return now < (self.current_started + 1800)
