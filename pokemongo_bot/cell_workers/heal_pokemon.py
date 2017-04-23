# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from __future__ import absolute_import

from datetime import datetime, timedelta
from pokemongo_bot.base_task import BaseTask
from pokemongo_bot.worker_result import WorkerResult
from pokemongo_bot import inventory
from pokemongo_bot.item_list import Item

class HealPokemon(BaseTask):
    SUPPORTED_TASK_API_VERSION = 1

    def __init__(self, bot, config):
        super(HealPokemon, self).__init__(bot, config)
        self.bot = bot
        self.config = config
        self.enabled = self.config.get("enabled", False)
        self.revive_pokemon = self.config.get("revive", True)
        self.heal_pokemon = self.config.get("heal", True)
        self.next_update = None

    def work(self):

        if not self.enabled:
            return WorkerResult.SUCCESS

        # Check for pokemon to heal or revive
        to_heal = []
        to_revive = []
        pokemons = inventory.pokemons().all()
        pokemons.sort(key=lambda p: p.hp)
        for pokemon in pokemons:
            if pokemon.hp < 1.0:
                to_revive += [pokemon]
            if pokemon.hp < pokemon.hp_max:
                to_heal += [pokemon]

        if len(to_heal) == 0 and len(to_revive) == 0:
            if self._should_print:
                self.next_update = datetime.now() + timedelta(seconds=120)
                #self.logger.info("No pokemon to heal or revive")
            return WorkerResult.SUCCESS
        # Okay, start reviving pokemons
        # Check revives and potions
        revives = inventory.items().get(Item.ITEM_REVIVE.value).count
        max_revives = inventory.items().get(Item.ITEM_MAX_REVIVE.value).count
        normal = inventory.items().get(Item.ITEM_POTION.value).count
        super_p = inventory.items().get(Item.ITEM_SUPER_POTION.value).count
        hyper = inventory.items().get(Item.ITEM_HYPER_POTION.value).count
        max_p = inventory.items().get(Item.ITEM_MAX_POTION.value).count

        self.logger.info("Healing %s pokemon" % len(to_heal))
        self.logger.info("Reviving %s pokemon" % len(to_revive))

        if self.revive_pokemon:
            if len(to_revive) > 0 and revives == 0 and max_revives == 0:
                self.logger.info("No revives left! Can't revive %s pokemons." % len(to_revive))
            elif len(to_revive) > 0:
                self.logger.info("Reviving %s pokemon..." % len(to_revive))
                for pokemon in to_revive:
                    self._revive_pokemon(pokemon)

        if self.heal_pokemon:
            if len(to_heal) > 0 and (normal + super_p + hyper + max_p) == 0:
                self.logger.info("No potions left! Can't heal %s pokemon" % len(to_heal))
            elif len(to_heal) > 0:
                self.logger.info("Healing %s pokemon" % len(to_heal))
                for pokemon in to_heal:
                    self._heal_pokemon(pokemon)

        if self._should_print:
            self.next_update = datetime.now() + timedelta(seconds=120)
            self.logger.info("Done healing/reviving pokemon")

    def _revive_pokemon(self, pokemon):
        item = Item.ITEM_REVIVE.value
        amount = inventory.items().get(item).count
        if amount == 0:
            self.logger.info("No normal revives left, using MAX revive!")
            item = Item.ITEM_MAX_REVIVE.value

        amount = inventory.items().get(item).count
        if amount > 0:
            response_dict_revive = self.bot.api.use_item_revive(item_id=item, pokemon_id=pokemon.unique_id)
            action_delay(1, 2)
            if response_dict_revive:
                result = response_dict_revive.get('responses', {}).get('USE_ITEM_REVIVE', {}).get('result', 0)
                if result is 1:  # Request success
                    self.emit_event(
                        'revived_pokemon',
                        formatted='Revived {name}.',
                        data={
                            'name': pokemon.name
                        }
                    )
                    return True
                else:
                    self.emit_event(
                        'revived_pokemon',
                        level='error',
                        formatted='Failed to revive {name}!',
                        data={
                            'name': pokemon.name
                        }
                    )
                    return False

    def _heal_pokemon(self, pokemon):
        # normal = inventory.items().get(Item.ITEM_POTION.value).count
        # super_p = inventory.items().get(Item.ITEM_SUPER_POTION.value).count
        # hyper = inventory.items().get(Item.ITEM_HYPER_POTION.value).count
        max_p = inventory.items().get(Item.ITEM_MAX_POTION.value).count
        # Figure out how much healing needs to be done.
        hp_to_restore = pokemon.hp_max - pokemon.hp
        if hp_to_restore > 200 and max_p > 0:
            # We should use a MAX Potion
            self._use_potion(Item.ITEM_MAX_POTION.value, pokemon)
            return True
        # Okay, now we see to heal as effective as possible
        potions = [103, 102, 101]
        heals = [200, 50, 20]

        for item_id, max_heal in zip(potions, heals):
            if inventory.items().get(item_id).count > 0:
                while hp_to_restore > max_heal:
                    delay_action(1, 2)
                    # More than 200 to restore, use a hyper first
                    if self._use_potion(item_id, pokemon):
                        hp_to_restore -= max_heal
                    else:
                        return WorkerResult.ERROR

        # Now we use the least
        potion_id = 101 # Normals first
        while hp_to_restore > 0:
            action_delay(0, 2)
            if inventory.items().get(potion_id).count > 0:
                if potion_id == 104:
                    self.logger.info("Using MAX potion to heal a %s" % pokemon.name)
                if self._use_potion(potion_id, pokemon):
                    # To get the healing done, get index
                    hp_to_restore -= heals[potion_id - 101]
            elif potion_id < 104:
                potion_id += 1
            else:
                self.logger.info("Can't heal a %s" % pokemon.name)


    def _use_potion(self, potion_id, pokemon):
        response_dict_potion = self.bot.api.use_item_potion(item_id=potion_id, pokemon_id=pokemon.unique_id)
        if response_dict_potion:
            result = response_dict_potion.get('responses', {}).get('USE_ITEM_POTION', {}).get('result', 0)
            if result is 1:  # Request success
                self.emit_event(
                    'healing_pokemon',
                    formatted='Healing {name} ({hp}/{hp_max}).',
                    data={
                        'name': pokemon.name,
                        'hp': pokemon.hp,
                        'hp_max': pokemon.hp_max
                    }
                )
                return True
            else:
                self.emit_event(
                    'healing_pokemon',
                    level='error',
                    formatted='Failed to heal {name} ({hp}/{hp_max})!',
                    data={
                        'name': pokemon.name,
                        'hp': pokemon.hp,
                        'hp_max': pokemon.hp_max
                    }
                )
                return False

    def _should_print(self):
        """
        Returns a value indicating whether the pokemon should be displayed.
        :return: True if the stats should be displayed; otherwise, False.
        :rtype: bool
        """
        return self.next_update is None or datetime.now() >= self.next_update
