# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from __future__ import absolute_import

from datetime import datetime, timedelta
import sys
import time
import random
from random import uniform
from collections import Counter

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
        self.min_recheck = self.config.get('min_recheck', 30)
        self.max_recheck = self.config.get('max_recheck', 120)
        self.recheck = datetime.now()
        self.walker = self.config.get('walker', 'StepWalker')
        self.destination = None
        self.recent_gyms = []
        self.pokemons = []
        self.fort_pokemons = []
        self.expire_recent = 10
        self.next_expire = None
        self.dropped_gyms = []
        self.blacklist= []
        self.check_interval = 0
        self.gyms = []
        self.raid_gyms = dict()

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

        self.dropped_gyms = []
        for pokemon in self.fort_pokemons:
            self.dropped_gyms.append(pokemon.fort_id)

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

        if hasattr(self.bot, "hunter_locked_target") and self.bot.hunter_locked_target is not None:
            # Don't move to a gym when hunting for a Pokemon
            return WorkerResult.SUCCESS

        if self.destination is None:
            # Check if we are walking past a gym
            close_gyms = self.get_gyms_in_range()
            if len(close_gyms) > 0:
                # self.logger.info("Walking past a gym!")
                for gym in close_gyms:
                    if gym["id"] in self.dropped_gyms:
                        continue

                    gym_details = self.get_gym_details(gym)
                    if gym_details:
                        pokes = self._get_pokemons_in_gym(gym_details)
                        if len(pokes) == 6:
                            continue
                        if 'enabled' in gym:
                            if not gym['enabled']:
                                continue
                        if 'owned_by_team' in gym:
                            if gym["owned_by_team"] == team:
                                if 'gym_display' in gym:
                                    display = gym['gym_display']
                                    if 'slots_available' in display:
                                        self.logger.info("Gym has %s open spots!" % display['slots_available'])
                                        if display['slots_available'] > 0 and gym["id"] not in self.dropped_gyms:
                                            self.logger.info("Dropping pokemon in %s" % gym_details["name"])
                                            self.drop_pokemon_in_gym(gym, pokes)
                                            if self.destination is not None and gym["id"] == self.destination["id"]:
                                                self.destination = None
                                            return WorkerResult.SUCCESS
                            # else:
                            #     self.logger.info("Not on our team: %s" % gym['owned_by_team'])
                        else:
                            self.logger.info("Neutral gym? %s" % gym)

        if not self.should_run():
            return WorkerResult.SUCCESS

        if self.destination is None:
            gyms = self.get_gyms()
            if len(gyms) == 0:
                if len(self.recent_gyms) == 0:
                    self.logger.info("No Gyms in range to scan!")
                return WorkerResult.SUCCESS

            self.logger.info("Inspecting %s gyms." % len(gyms))
            self.logger.info("Recent gyms: %s" % len(self.recent_gyms))
            self.logger.info("Active raid gyms: %s" % len(self.raid_gyms))
            teams = []
            for gym in gyms:
                # Ignore after done for 5 mins
                self.recent_gyms.append(gym["id"])

                if 'enabled' in gym:
                    # self.logger.info("Gym enabled? %s" % gym)
                    # Gym can be closed for a raid or something, skipp to the next
                    if not gym['enabled']:
                        # self.logger.info("Yes, it's closed...")
                        continue

                # if 'type' in gym:
                #     self.logger.info("Type: %s" % gym["type"] )
                #     if gym["type"] == 1:
                #         self.logger.info("Seems to be a Pokestop?")
                #         continue

                if 'owned_by_team' in gym:
                    if gym["owned_by_team"] == 1:
                        teams.append("Mystic")
                    elif gym["owned_by_team"] == 2:
                        teams.append("Valor")
                    elif gym["owned_by_team"] == 3:
                        teams.append("Instinct")
                    # else:
                    #     self.logger.info("Unknown team? %s" % gym)

                    if gym["owned_by_team"] == team:
                        if 'gym_display' in gym:
                            display = gym['gym_display']
                            if 'slots_available' in display:
                                self.logger.info("Gym has %s open spots!" % display['slots_available'])
                                self.destination = gym
                                break
                else:
                    # self.logger.info("Found a Neutral gym?")
                    # self.logger.info("Info: %s" % gym)
                    self.destination = gym
                    break
            if len(teams) > 0:
                count_teams = Counter(teams)
                self.logger.info("Gym Teams %s", ", ".join('{}({})'.format(key, val) for key, val in count_teams.items()))

        if self.destination is not None:
            # self.logger.info("Check interval: %s" % self.check_interval )
            if self.check_interval >= 4:
                self.check_interval = 0
                gyms = self.get_gyms()
                for g in gyms:
                    if g["id"] == self.destination["id"]:
                        # self.logger.info("Inspecting target: %s" % g)
                        if "owned_by_team" in g and g["owned_by_team"] is not team:
                            self.logger.info("Damn! Team %s took gym before we arrived!" % g["owned_by_team"])
                            self.destination = None
                            return WorkerResult.SUCCESS
                        break
            else:
                self.check_interval += 1

            # Moving to a gym to deploy Pokemon
            unit = self.bot.config.distance_unit  # Unit to use when printing formatted distance
            lat = self.destination["latitude"]
            lng = self.destination["longitude"]
            details = fort_details(self.bot, self.destination["id"], lat, lng)
            gym_name = details.get('name', 'Unknown')

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
                    'fort_name': u"{}".format(gym_name),
                    'distance': format_dist(dist, unit),
                }
                self.emit_event(
                    'moving_to_fort',
                    formatted="Moving towards Gym {fort_name} - {distance}",
                    data=fort_event_data
                )

                step_walker = walker_factory(self.walker, self.bot, lat, lng)

                if not step_walker.step():
                    return WorkerResult.RUNNING
            else:
                self.emit_event(
                    'arrived_at_fort',
                    formatted=("Arrived at Gym %s." % gym_name)
                )
                gym_details = self.get_gym_details(self.destination)
                current_pokemons = self._get_pokemons_in_gym(gym_details)
                self.drop_pokemon_in_gym(self.destination, current_pokemons)
                self.destination = None
                return WorkerResult.SUCCESS
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
        self.pokemons = inventory.pokemons().all()
        self.fort_pokemons = [p for p in self.pokemons if p.in_fort]
        self.pokemons = [p for p in self.pokemons if not p.in_fort]
        close_gyms = self.get_gyms_in_range()

        empty_gym = False
    
        for g in close_gyms:
            if g["id"] == gym["id"]:
                if 'owned_by_team' in g:
                    self.logger.info("Expecting team: %s it is: %s" % (self.bot.player_data['team'], g["owned_by_team"]) )
                    if g["owned_by_team"] is not self.bot.player_data['team']:
                        self.logger.info("Can't drop in a enemy gym!")
                        self.recent_gyms.append(gym["id"])
                        return WorkerResult.SUCCESS
                else:
                    self.logger.info("Empty gym?? %s" % g)
                    gym_details = self.get_gym_details(gym)
                    self.logger.info("Details: %s" % gym_details)
                    empty_gym = True
                    if not gym_details or gym_details == {}:
                        self.logger.info("No details for this Gym? Blacklisting!")
                        self.blacklist.append(gym["id"])
                        return WorkerResult.SUCCESS

        # Check for raid
        if 'raid_info' in gym:
            raid_info = gym["raid_info"]
            raid_starts = datetime.fromtimestamp(int(raid_info["raid_battle_ms"]) / 1e3)
            raid_ends = datetime.fromtimestamp(int(raid_info["raid_end_ms"]) / 1e3)
            self.logger.info("Raid starts: %s" % raid_starts.strftime('%Y-%m-%d %H:%M:%S.%f'))
            self.logger.info("Raid ends: %s" % raid_ends.strftime('%Y-%m-%d %H:%M:%S.%f'))
            t = datetime.today()

            if raid_starts < datetime.now():
                self.logger.info("Active raid?")
                if raid_ends < datetime.now():
                    self.logger.info("No need to wait.")
                elif (raid_ends-t).seconds > 600:
                    self.logger.info("Need to wait long than 10 minutes, skipping")
                    self.destination = None
                    self.recent_gyms.append(gym["id"])
                    self.raid_gyms[gym["id"]] = raid_ends
                    return WorkerResult.SUCCESS
                else:
                    self.logger.info("Waiting for %s seconds for raid to end..." % (raid_ends-t).seconds)
                    sleep((raid_ends-t).seconds)
            else:
                self.logger.info("Raid has not begun yet!")

        for pokemon in self.fort_pokemons:
            if pokemon.fort_id == gym["id"]:
                self.logger.info("We are already in this gym!")
                self.dropped_gyms.append(pokemon.fort_id)
                self.recent_gyms.append(gym["id"])
                return WorkerResult.SUCCESS

        if 'same_team_deploy_lockout_end_ms' in gym:
            # self.logger.info("%f" % gym["same_team_deploy_lockout_end_ms"])
            org_time = int(gym["same_team_deploy_lockout_end_ms"]) / 1e3
            self.logger.info("org_time: %f" % org_time)
            lockout_time = datetime.fromtimestamp(org_time)
            self.logger.info("Lockout time: %s" % lockout_time.strftime('%Y-%m-%d %H:%M:%S.%f'))
            t = datetime.today()

            if lockout_time < datetime.now():
                self.logger.info("No need to wait.")
            else:
                self.logger.info("Waiting for %s seconds deployment lockout to end..." % (lockout_time-t).seconds)
                sleep((lockout_time-t).seconds)

        #FortDeployPokemon
        # self.logger.info("Trying to deploy Pokemon in gym: %s" % gym)
        gym_details = self.get_gym_details(gym)
        # self.logger.info("Gym details: %s" % gym_details)
        fort_pokemon = self._get_best_pokemon(current_pokemons)
        pokemon_id = fort_pokemon.unique_id
        # self.logger.info("Trying to deploy %s (%s)" % (fort_pokemon, pokemon_id))
        # self.logger.info("Gym in control by %s. I am on team %s" % (gym["owned_by_team"], self.bot.player_data['team']))

        request = self.bot.api.create_request()
        request.gym_deploy(
            fort_id=gym["id"],
            pokemon_id=pokemon_id,
            player_latitude=f2i(self.bot.position[0]),
            player_longitude=f2i(self.bot.position[1])
        )
        # self.logger.info("Req: %s" % request)
        response_dict = request.call()
        self.logger.info("Called deploy pokemon: %s" % response_dict)

        if ('responses' in response_dict) and ('GYM_DEPLOY' in response_dict['responses']):
            deploy = response_dict['responses']['GYM_DEPLOY']
            result = response_dict.get('status_code', -1)
            self.recent_gyms.append(gym["id"])
            # self.logger.info("Status: %s" % result)
            if result == 1:
                self.dropped_gyms.append(gym["id"])
                gym_details = self.get_gym_details(gym)
                # SUCCES
                self.logger.info("We deployed %s (%s CP) in the gym!" % (fort_pokemon.name, fort_pokemon.cp))
                self.emit_event(
                    'deployed_pokemon',
                    formatted=("We deployed %s (%s CP) in the gym %s!!" % (fort_pokemon.name, fort_pokemon.cp, gym_details["name"])),
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
        if len(self.gyms) == 0:
            self.gyms = self.bot.get_gyms(order_by_distance=True)

        if self._should_recheck():
            self.gyms = self.bot.get_gyms(order_by_distance=True)
            self._compute_next_recheck()

        if self._should_expire():
            self.recent_gyms = []
            self._compute_next_expire()
        # Check raid gyms for raids that ended
        for gym_id in self.raid_gyms:
            if self.raid_gyms[gym_id] < datetime.now():
                self.logger.info("Raid at %s ended (%s)" % (gym_id, self.raid_gyms[gym_id]))
                del(self.raid_gyms[gym_id])

        gyms = []
        # if not skip_recent_filter:
        gyms = filter(lambda gym: gym["id"] not in self.recent_gyms, self.gyms)
        # Filter blacklisted gyms
        gyms = filter(lambda gym: gym["id"] not in self.blacklist, gyms)
        # Filter out gyms we already in
        gyms = filter(lambda gym: gym["id"] not in self.dropped_gyms, gyms)
        # Filter ongoing raids
        gyms = filter(lambda gym: gym["id"] not in self.raid_gyms, gyms)
        # filter fake gyms
        # self.gyms = filter(lambda gym: "type" not in gym or gym["type"] != 1, self.gyms)
        # sort by current distance
        gyms.sort(key=lambda x: distance(
                self.bot.position[0],
                self.bot.position[1],
                x['latitude'],
                x['longitude']
            ))

        return gyms

    def get_gyms_in_range(self):
        gyms = self.get_gyms()

        if self.bot.config.replicate_gps_xy_noise:
            gyms = filter(lambda fort: distance(
                self.bot.noised_position[0],
                self.bot.noised_position[1],
                fort['latitude'],
                fort['longitude']
            ) <= Constants.MAX_DISTANCE_FORT_IS_REACHABLE, self.gyms)
        else:
            gyms = filter(lambda fort: distance(
                self.bot.position[0],
                self.bot.position[1],
                fort['latitude'],
                fort['longitude']
            ) <= Constants.MAX_DISTANCE_FORT_IS_REACHABLE, self.gyms)

        return gyms

    def _should_print(self):
        return self.next_update is None or datetime.now() >= self.next_update

    def _should_expire(self):
        return self.next_expire is None or datetime.now() >= self.next_expire

    def _compute_next_expire(self):
        self.next_expire = datetime.now() + timedelta(seconds=300)

    def _compute_next_recheck(self):
        wait = uniform(self.min_recheck, self.max_recheck)
        self.recheck = datetime.now() + timedelta(seconds=wait)

    def _should_recheck(self):
        return self.recheck is None or datetime.now() >= self.recheck

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
        # HP Must be max
        possible_pokemons = [p for p in possible_pokemons if p.hp == p.hp_max]
        possible_pokemons = [p for p in possible_pokemons if not p.in_fort]
        # Sort them
        pokemons_ordered = sorted(possible_pokemons, key=lambda x: get_poke_info(self.order_by, x), reverse=True)
        # Top 10 picks
        pokemons_ordered = pokemons_ordered[0:10]
        # Pick a random one!
        random.shuffle(pokemons_ordered)
        return pokemons_ordered[0]
