# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from __future__ import absolute_import

from datetime import datetime, timedelta
import sys
import time

from pgoapi.utilities import f2i
from pokemongo_bot import inventory
from pokemongo_bot.inventory import player

from pokemongo_bot.constants import Constants
from pokemongo_bot.human_behaviour import action_delay, sleep
from pokemongo_bot.worker_result import WorkerResult
from pokemongo_bot.base_task import BaseTask
from pokemongo_bot import inventory
from .utils import distance, format_time, fort_details
from pokemongo_bot.tree_config_builder import ConfigException

GYM_DETAIL_RESULT_SUCCESS = 1
GYM_DETAIL_RESULT_OUT_OF_RANGE = 2
GYM_DETAIL_RESULT_UNSET = 0


class GymPokemon(BaseTask):
    SUPPORTED_TASK_API_VERSION = 1

    def __init__(self, bot, config):
        super(GymPokemon, self).__init__(bot, config)

    def initialize(self):
        # 10 seconds from current time
        self.next_update = datetime.now() + timedelta(0, 10)
        self.order_by = self.config.get('order_by', 'cp')
        self.min_interval = self.config.get('min_interval', 60)
        self.recent_gyms = []
        self.pokemons = []
        self.fort_pokemons = []

    def should_run(self):
        # Check if we have any Pokemons and are level > 5
        return player()._level >= 5 and len(self.pokemons) > 0

    def display_fort_pokemon(self):
        if len(self.fort_pokemons) == 0:
            return
        self.logger.info("We currently have %s Pokemon in Gym(s)" % len(self.fort_pokemons) )
        for pokemon in self.fort_pokemons:
            lat = self.bot.position[0:2][0]
            lng = self.bot.position[0:2][1]
            details = fort_details(self.bot, pokemon.fort_id, lat, lng)
            fort_name = details.get('name', 'Unknown')
            self.logger.info("%s: %s (%s CP)" % (fort_name, pokemon.name, pokemon.cp))
        present = datetime.now()
        collection_datetime = self.bot.next_gym_collection_time()
        if collection_datetime < present:
            # We can collect!!
            self.logger.info("We can collect our daily bonus!!")
            self.logger.info("Collection date/time: %s" % collection_datetime.strftime("%Y/%m/%d %H:%M:%S"))
        else:
            self.logger.info("Next collection at %s" % collection_datetime.strftime("%Y/%m/%d %H:%M:%S"))

    def work(self):
        self.pokemons = inventory.pokemons().all()
        self.fort_pokemons = [p for p in self.pokemons if p.in_fort]
        self.pokemons = [p for p in self.pokemons if not p.in_fort]
        # self.pokemons = [pokemon
        #                  for pokemon in self.pokemons
        #                  if pokemon['deployed_fort_id'] == None]
        #deployed_fort_id
        gyms = self.get_gyms_in_range()

        if self._should_print():
            self.display_fort_pokemon()
            self._compute_next_update()
        # Do display teh stats about Pokemon in Gym and collection time [please]
        if not self.enabled:
            return WorkerResult.SUCCESS

        if not self.should_run() or len(gyms) == 0:
            return WorkerResult.SUCCESS

        gym = gyms[0]
        # Ignore after done for 5 mins
        self.bot.fort_timeouts[gym["id"]] = (time.time() + 300) * 1000
        self.bot.recent_forts = self.bot.recent_forts[1:] + [gym['id']]

        team = self.bot.player_data['team']
        if 'owned_by_team' not in gym:
            self.logger.info("Empty gym found!!")
            self.drop_pokemon_in_gym(gym, [])
            if len(gyms) > 1:
                return WorkerResult.RUNNING
            else:
                return WorkerResult.SUCCESS
        elif not gym["owned_by_team"] == team:
            self.logger.info("Not owned by own team")
            if len(gyms) > 1:
                return WorkerResult.RUNNING
            else:
                return WorkerResult.SUCCESS

        gym_details = self.get_gym_details(gym)

        if gym_details:
            points = gym['gym_points']
            # We got the data
            self.logger.info("Deploy lockout: %s" % gym_details['deploy_lockout'])
            count = 1
            gym_current_pokemon = []
            for member in gym_details['memberships']:
                poke = inventory.Pokemon(member.get('pokemon_data'))
                gym_current_pokemon += [poke]
                self.logger.info("%s: %s (%s CP)" % (count, poke.name, poke.cp))
                count += 1
            max_mons = 1
            # Case statment on points to see if there is room.
            if points >= 50000:
                max_mons = 10
            elif points >= 40000:
                # Max 9
                max_mons = 9
            elif points >= 30000:
                max_mons = 8
            elif points >= 20000:
                max_mons = 7
            elif points >= 16000:
                max_mons = 6
            elif points >= 12000:
                max_mons = 5
            elif points >= 8000:
                max_mons = 4
            elif points >= 4000:
                max_mons = 3
            elif points >= 2000:
                max_mons = 2
            # Is there room?
            if len(gym_details['memberships']) < max_mons:
                # there is room!
                # make sure we don't drop a Pokemon too soon (Saskia will kill me!)
                # Check for a lockout
                while gym_details['deploy_lockout'] is not None:
                    self.logger.info("Deploy lockout active!! Wating to make sure we don't grab a spot from hard working team mates!!")
                    action_delay(3, 10)
                    # Grab the details again to see if there is still room
                    gym_details = self.get_gym_details(gym)

                if len(gym_details['memberships']) < max_mons:
                    # Still room enough!
                    self.drop_pokemon_in_gym(gym, gym_current_pokemon)
                else:
                    self.logger.info("Team member entered gym before us, nice!")
            else:
                self.emit_event(
                    'gym_full',
                    formatted=("Gym is full, can not add a Pokemon!" )
                )
            #

        if len(gyms) > 1:
            return WorkerResult.RUNNING

        return WorkerResult.SUCCESS

    def get_gym_details(self, gym):
        lat = gym['latitude']
        lng = gym['longitude']

        details = fort_details(self.bot, gym['id'], lat, lng)
        fort_name = details.get('name', 'Unknown')

        self.logger.info("Checking Gym: %s (%s pts)" % (fort_name, gym['gym_points']))

        response_dict = self.bot.api.get_gym_details(
            gym_id=gym['id'],
            gym_latitude=lat,
            gym_longitude=lng,
            player_latitude=f2i(self.bot.position[0]),
            player_longitude=f2i(self.bot.position[1]),
            client_version='0.67.1'
        )

        if ('responses' in response_dict) and ('GET_GYM_DETAILS' in response_dict['responses']):
            gym_details = response_dict['responses']['GET_GYM_DETAILS']
            detail_result = gym_details.get('result', -1)
            if detail_result == GYM_DETAIL_RESULT_SUCCESS:
                details = dict()
                details['points'] = gym['gym_points']
                # We got the data
                details['state'] = gym_details.get('gym_state')
                details['memberships'] = details['state'].get('memberships')
                details['deploy_lockout'] = gym_details.get('deploy_lockout')

                return details
            else:
                return False
        else:
            return False

    def drop_pokemon_in_gym(self, gym, current_pokemons):
        #FortDeployPokemon
        fort_pokemon = self._get_best_pokemon(current_pokemons)
        pokemon_id = fort_pokemon.unique_id
        lat = gym['latitude']
        lng = gym['longitude']
        response_dict = self.bot.api.fort_deploy_pokemon(
            fort_id=gym['id'],
            pokemon_id=pokemon_id,
            gym_latitude=lat,
            gym_longitude=lng,
            player_latitude=f2i(self.bot.position[0]),
            player_longitude=f2i(self.bot.position[1])
        )
        if ('responses' in response_dict) and ('FORT_DEPLOY_POKEMON' in response_dict['responses']):
            deploy = response_dict['responses']['FORT_DEPLOY_POKEMON']
            result = deploy.get('result', -1)
            if result == 1:
                # SUCCES
                self.logger.info("We deployed %(name)s (%(cp)s CP) in the gym!", fort_pokemon)
                self.emit_event(
                    'deployed_pokemon',
                    formatted="We dropped a pokemon in a gym!!",
                    data={'gym_id': gym['id'], 'pokemon_id': pokemon_id}
                )
                return WorkerResult.SUCCESS
            elif result == 2:
                #ERROR_ALREADY_HAS_POKEMON_ON_FORT
                self.logger.info('ERROR_ALREADY_HAS_POKEMON_ON_FORT')
                return WorkerResult.ERROR
            elif result == 3:
                #ERROR_OPPOSING_TEAM_OWNS_FORT
                self.logger.info('ERROR_OPPOSING_TEAM_OWNS_FORT')
                return WorkerResult.ERROR
            elif result == 4:
                #ERROR_FORT_IS_FULL
                self.logger.info('ERROR_FORT_IS_FULL')
                return WorkerResult.ERROR
            elif result == 5:
                #ERROR_NOT_IN_RANGE
                self.logger.info('ERROR_NOT_IN_RANGE')
                return WorkerResult.ERROR
            elif result == 6:
                #ERROR_PLAYER_HAS_NO_TEAM
                self.logger.info('ERROR_PLAYER_HAS_NO_TEAM')
                return WorkerResult.ERROR
            elif result == 7:
                #ERROR_POKEMON_NOT_FULL_HP
                self.logger.info('ERROR_POKEMON_NOT_FULL_HP')
                return WorkerResult.ERROR
            elif result == 8:
                #ERROR_PLAYER_BELOW_MINIMUM_LEVEL
                self.logger.info('ERROR_PLAYER_BELOW_MINIMUM_LEVEL')
                return WorkerResult.ERROR
            elif result == 8:
                #ERROR_POKEMON_IS_BUDDY
                self.logger.info('ERROR_POKEMON_IS_BUDDY')
                return WorkerResult.ERROR

    def get_gyms_in_range(self):
        gyms = self.bot.get_gyms(order_by_distance=True)
        gyms = filter(lambda gym: gym["id"] not in self.bot.recent_forts, gyms)

        if self.bot.config.replicate_gps_xy_noise:
            gyms = filter(lambda fort: distance(
                self.bot.noised_position[0],
                self.bot.noised_position[1],
                fort['latitude'],
                fort['longitude']
            ) <= Constants.MAX_DISTANCE_FORT_IS_REACHABLE, gyms)
        else:
            gyms = filter(lambda fort: distance(
                self.bot.position[0],
                self.bot.position[1],
                fort['latitude'],
                fort['longitude']
            ) <= Constants.MAX_DISTANCE_FORT_IS_REACHABLE, gyms)

        if len(gyms) > 0:
            self.logger.info("Found %s gyms!", len(gyms))

        return gyms

    def _should_print(self):
        return self.next_update is None or datetime.now() >= self.next_update

    def _compute_next_update(self):
        """
        Computes the next update datetime based on the minimum update interval.
        :return: Nothing.
        :rtype: None
        """
        self.next_update = datetime.now() + timedelta(seconds=self.min_interval)

    def _get_best_pokemon(self, current_pokemons):
        def get_poke_info(info, pokemon):
            poke_info = {
                'cp': pokemon.cp,
                'iv': pokemon.iv,
                'ivcp': pokemon.ivcp,
                'ncp': pokemon.cp_percent,
                'level': pokemon.level,
                'hp': pokemon.hp,
                'dps': pokemon.moveset.dps
            }
            if info not in poke_info:
                raise ConfigException("order by {}' isn't available".format(self.order_by))
            return poke_info[info]
        # Don't place a Pokemon which is already in the gym (prevent ALL Blissey etc)
        possible_pokemons = [p for p in self.pokemons if not p in current_pokemons]
        pokemons_ordered = sorted(possible_pokemons, key=lambda x: get_poke_info(self.order_by, x), reverse=True)
        return pokemons_ordered[0]
