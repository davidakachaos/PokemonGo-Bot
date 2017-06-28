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
from .utils import distance, format_time, fort_details, format_dist
from pokemongo_bot.tree_config_builder import ConfigException
from pokemongo_bot.walkers.walker_factory import walker_factory
from pokemongo_bot.inventory import Pokemons

GYM_DETAIL_RESULT_SUCCESS = 1
GYM_DETAIL_RESULT_OUT_OF_RANGE = 2
GYM_DETAIL_RESULT_UNSET = 0

TEAM_BLUE = 1
TEAM_RED = 2
TEAM_YELLOW = 3


class GymPokemon(BaseTask):
    SUPPORTED_TASK_API_VERSION = 1

    def __init__(self, bot, config):
        super(GymPokemon, self).__init__(bot, config)

    def initialize(self):
        # 10 seconds from current time
        self.next_update = datetime.now() + timedelta(0, 10)
        self.order_by = self.config.get('order_by', 'cp')
        self.min_interval = self.config.get('min_interval', 60)
        self.walker = self.config.get('walker', 'StepWalker')
        self.destination = None
        self.recent_gyms = []
        self.pokemons = []
        self.fort_pokemons = []
        self.expire_recent = 10
        self.next_expire = None
        self.dropped_gyms = []

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

    def work(self):
        self.pokemons = inventory.pokemons().all()
        self.fort_pokemons = [p for p in self.pokemons if p.in_fort]
        self.pokemons = [p for p in self.pokemons if not p.in_fort]
        team = self.bot.player_data['team']

        if self._should_print():
            self.display_fort_pokemon()
            self._compute_next_update()
        # Do display teh stats about Pokemon in Gym and collection time [please]
        if not self.enabled:
            return WorkerResult.SUCCESS

        if len(self.fort_pokemons) >= 20:
            if self._should_print():
                self.logger.info("We have a max of 20 Pokemon in gyms.")
            return WorkerResult.SUCCESS

        # Check if we are walking past a gym
        close_gyms = self.get_gyms_in_range()
        if len(close_gyms) > 0:
            self.logger.info("Walking past a gym!")
            for gym in close_gyms:
                gym_details = self.get_gym_details(gym)
                if gym_details:
                    pokes = self._get_pokemons_in_gym(gym_details)
                    if len(pokes) == 6:
                        # self.logger.info("Gym full of Pokemon")
                        continue
                    if 'enabled' in gym:
                        if not gym['enabled']:
                            continue
                    if 'owned_by_team' in gym:
                        if gym["owned_by_team"] == team:
                            self.logger.info("Gym on our team!")
                            self.logger.info("Pokemons in Gym: %s" % pokes)
                            # self.logger.info("Gym: %s" % gym)
                            # self.logger.info("Details: %s" % gym_details)
                            if 'gym_display' in gym:
                                display = gym['gym_display']
                                if 'slots_available' in display:
                                    self.logger.info("Gym has %s open spots!" % display['slots_available'])
                                    if display['slots_available'] > 0 and gym["id"] not in self.dropped_gyms:
                                        # gym_details = self.get_gym_details(self.destination)
                                        # current_pokemons = self._get_pokemons_in_gym(gym_details)
                                        self.logger.info("Dropping pokemon in %s" % gym_details["name"])
                                        self.drop_pokemon_in_gym(self.destination, pokes)
                        else:
                            self.logger.info("Not on our team: %s" % gym['owned_by_team'])
                    else:
                        self.logger.info("Neutral gym? %s" % gym)

        if hasattr(self.bot, "hunter_locked_target") and self.bot.hunter_locked_target is not None:
            return WorkerResult.SUCCESS

        if not self.should_run():
            return WorkerResult.SUCCESS

        if self.destination is None:
            gyms = self.get_gyms()
            if len(gyms) == 0:
                return WorkerResult.SUCCESS

            self.logger.info("Inspecting %s gyms." % len(gyms))
            self.logger.info("Recent gyms: %s" % len(self.recent_gyms))

            for gym in gyms:
                # Ignore after done for 5 mins
                self.recent_gyms.append(gym["id"])

                if 'enabled' in gym:
                    # self.logger.info("Gym enabled? %s" % gym)
                    # Gym can be closed for a raid or something, skipp to the next
                    if not gym['enabled']:
                        # self.logger.info("Yes, it's closed...")
                        continue

                if 'owned_by_team' in gym:
                    self.logger.info("Found gym controlled by %s" % gym["owned_by_team"])
                    if gym["owned_by_team"] == team:
                        self.logger.info("Gym on our team!")
                        if 'gym_display' in gym:
                            display = gym['gym_display']
                            if 'slots_available' in display:
                                self.logger.info("Gym has %s open spots!" % display['slots_available'])
                                self.destination = gym
                                break
                else:
                    self.logger.info("Found a Neutral gym?")
                    self.logger.info("Info: %s" % gym)
                    self.destination = gym
                    break


        if self.destination is not None:
            # Moving to a gym to deploy Pokemon
            unit = self.bot.config.distance_unit  # Unit to use when printing formatted distance
            lat = self.destination["latitude"]
            lng = self.destination["longitude"]

            dist = distance(
                self.bot.position[0],
                self.bot.position[1],
                lat,
                lng
            )
            noised_dist = distance(
                self.bot.noised_position[0],
                self.bot.noised_position[1],
                lat,
                lng
            )

            moving = noised_dist > Constants.MAX_DISTANCE_FORT_IS_REACHABLE if self.bot.config.replicate_gps_xy_noise else dist > Constants.MAX_DISTANCE_FORT_IS_REACHABLE

            if moving:
                fort_event_data = {
                    'fort_name': u"{}".format(""),
                    'distance': format_dist(dist, unit),
                }
                self.emit_event(
                    'moving_to_fort',
                    formatted="Moving towards Gym {fort_name} - {distance}",
                    data=fort_event_data
                )

                step_walker = walker_factory(self.walker,
                    self.bot,
                    lat,
                    lng
                )

                if not step_walker.step():
                    return WorkerResult.RUNNING
            else:
                self.emit_event(
                    'arrived_at_fort',
                    formatted='Arrived at Gym.'
                )
                gym_details = self.get_gym_details(self.destination)
                current_pokemons = self._get_pokemons_in_gym(gym_details)
                self.drop_pokemon_in_gym(self.destination, current_pokemons)
                self.destination = None
            # return WorkerResult.RUNNING

        return WorkerResult.SUCCESS

    def get_gym_details(self, gym):
        lat = gym['latitude']
        lng = gym['longitude']

        in_reach = False

        if self.bot.config.replicate_gps_xy_noise:
            if distance(self.bot.noised_position[0], self.bot.noised_position[1], gym['latitude'], gym['longitude']) <= Constants.MAX_DISTANCE_FORT_IS_REACHABLE:
                in_reach = True
        else:
            if distance(self.bot.position[0], self.bot.position[1], gym['latitude'], gym['longitude']) <= Constants.MAX_DISTANCE_FORT_IS_REACHABLE:
                in_reach = True

        if in_reach:
            request = self.bot.api.create_request()
            request.gym_get_info(gym_id=gym['id'], gym_lat_degrees=lat, gym_lng_degrees=lng, player_lat_degrees=self.bot.position[0],player_lng_degrees=self.bot.position[1])
            response_dict = request.call()

            if ('responses' in response_dict) and ('GYM_GET_INFO' in response_dict['responses']):
                details = response_dict['responses']['GYM_GET_INFO']
                return details
        else:
            return False
        # details = fort_details(self.bot, , lat, lng)
        # fort_name = details.get('name', 'Unknown')
        # self.logger.info("Checking Gym: %s (%s pts)" % (fort_name, gym['gym_points']))

    def _get_pokemons_in_gym(self, gym_details):
        pokemon_names = []
        gym_info = gym_details.get('gym_status_and_defenders', None)
        if gym_info:
            defenders = gym_info.get('gym_defender', [])
            for defender in defenders:
                motivated_pokemon = defender.get('motivated_pokemon')
                pokemon_info = motivated_pokemon.get('pokemon')
                pokemon_id = pokemon_info.get('pokemon_id')
                pokemon_names.append(Pokemons.name_for(pokemon_id))

        return pokemon_names

    def drop_pokemon_in_gym(self, gym, current_pokemons):
        #FortDeployPokemon
        self.logger.info("Trying to deploy Pokemon in gym.")
        fort_pokemon = self._get_best_pokemon(current_pokemons)
        pokemon_id = fort_pokemon.unique_id
        self.logger.info("Trying to deploy %s (%s)" % (fort_pokemon, pokemon_id))

        request = self.bot.api.create_request()
        request.fort_deploy_pokemon(
            fort_id=gym['id'],
            pokemon_id=pokemon_id,
            player_latitude=f2i(self.bot.position[0]),
            player_longitude=f2i(self.bot.position[1])
        )
        response_dict = request.call()
        self.logger.info("Called deploy pokemon: %s" % response_dict)

        if ('responses' in response_dict) and ('FORT_DEPLOY_POKEMON' in response_dict['responses']):
            deploy = response_dict['responses']['FORT_DEPLOY_POKEMON']
            result = response_dict.get('status_code', -1)
            self.logger.info("Status: %s" % result)
            if result == 1:
                self.dropped_gyms.append(gym["id"])
                # SUCCES
                self.logger.info("We deployed %s (%s CP) in the gym!" % (fort_pokemon.name, fort_pokemon.cp))
                self.emit_event(
                    'deployed_pokemon',
                    formatted="We dropped a %s in a gym!!".format(fort_pokemon.name),
                    data={'gym_id': gym['id'], 'pokemon_id': pokemon_id}
                )
                return WorkerResult.SUCCESS
            elif result == 2:
                #ERROR_ALREADY_HAS_POKEMON_ON_FORT
                self.logger.info('ERROR_ALREADY_HAS_POKEMON_ON_FORT')
                self.dropped_gyms.append(gym["id"])
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

    def get_gyms(self, skip_recent_filter=False):
        gyms = self.bot.get_gyms(order_by_distance=True)
        if self._should_expire():
            self.recent_gyms = []
            self._compute_next_expire()
        if not skip_recent_filter:
            gyms = filter(lambda gym: gym["id"] not in self.recent_gyms, gyms)
        return gyms

    def get_gyms_in_range(self):
        gyms = self.get_gyms(True)
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

        return gyms

    def _should_print(self):
        return self.next_update is None or datetime.now() >= self.next_update

    def _should_expire(self):
        return self.next_expire is None or datetime.now() >= self.next_expire

    def _compute_next_expire(self):
        self.next_expire = datetime.now() + timedelta(seconds=3600)

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
        possible_pokemons = [p for p in self.pokemons if not p.name in current_pokemons]
        # Don't put in Pokemon above 3000 cp (morale drops too fast)
        possible_pokemons = [p for p in possible_pokemons if p.cp < 3000]
        # Sort them
        pokemons_ordered = sorted(possible_pokemons, key=lambda x: get_poke_info(self.order_by, x), reverse=True)
        return pokemons_ordered[0]
