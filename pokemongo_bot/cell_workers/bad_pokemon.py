from pokemongo_bot import inventory
from pokemongo_bot.inventory import Pokemon
from pokemongo_bot.inventory import Pokemons
from pokemongo_bot.human_behaviour import sleep, action_delay
from pokemongo_bot.base_task import BaseTask
from pokemongo_bot.worker_result import WorkerResult

class BadPokemon(BaseTask):
  SUPPORTED_TASK_API_VERSION = 1

  def __init__(self, bot, config):
    super(TransferPokemon, self).__init__(bot, config)

  def initialize(self):
    self.config_transfer = self.config.get('transfer', False)
    self.config_bulktransfer_enabled = self.config.get('bulktransfer_enabled', True)
    self.config_action_wait_min = self.config.get("action_wait_min", 3)
    self.config_action_wait_max = self.config.get("action_wait_max", 5)

  def work(self):
    bad_pokemons = [p for p in inventory.pokemons().all() if p.is_bad]
    
    if len(bad_pokemons) > 0:
      self.logger.warning("You have %s bad (slashed) Pokemon!" % len(bad_pokemons))
      sleep(5)
      if self.config_transfer:
        self.transfer_pokemon(bad_pokemons)

    return WorkerResult.SUCCESS

  def transfer_pokemon(self, pokemons, skip_delay=False):
        error_codes = {
            0: 'UNSET',
            1: 'SUCCESS',
            2: 'POKEMON_DEPLOYED',
            3: 'FAILED',
            4: 'ERROR_POKEMON_IS_EGG',
            5: 'ERROR_POKEMON_IS_BUDDY'
        }
        if self.config_bulktransfer_enabled and len(pokemons) > 1:
            while len(pokemons) > 0:
                action_delay(self.config_action_wait_min, self.config_action_wait_max)
                pokemon_ids = []
                count = 0
                transfered = []
                while len(pokemons) > 0 and count < self.config_max_bulktransfer:
                    pokemon = pokemons.pop()
                    transfered.append(pokemon)
                    pokemon_ids.append(pokemon.unique_id)
                    count = count + 1
                try:
                    if self.config_transfer:
                        request = self.bot.api.create_request()
                        request.release_pokemon(pokemon_ids=pokemon_ids)
                        response_dict = request.call()
                        
                        result = response_dict['responses']['RELEASE_POKEMON']['result']
                        if result != 1:
                            self.logger.error(u'Error while transfer pokemon: {}'.format(error_codes[result]))
                            return False
                except Exception:
                    return False

                for pokemon in transfered:
                    candy = inventory.candies().get(pokemon.pokemon_id)

                    if self.config_transfer and (not self.bot.config.test):
                        candy.add(1)

                    self.emit_event("pokemon_release",
                                    formatted="Exchanged {pokemon} [IV {iv}] [CP {cp}] [{candy} candies]",
                                    data={"pokemon": pokemon.name,
                                          "iv": pokemon.iv,
                                          "cp": pokemon.cp,
                                          "candy": candy.quantity})

                    if self.config_transfer:
                        inventory.pokemons().remove(pokemon.unique_id)

                        with self.bot.database as db:
                            cursor = db.cursor()
                            cursor.execute("SELECT COUNT(name) FROM sqlite_master WHERE type='table' AND name='transfer_log'")

                            db_result = cursor.fetchone()

                            if db_result[0] == 1:
                                db.execute("INSERT INTO transfer_log (pokemon, iv, cp) VALUES (?, ?, ?)", (pokemon.name, pokemon.iv, pokemon.cp))

        else:
            for pokemon in pokemons:
                if self.config_transfer and (not self.bot.config.test):
                    request = self.bot.api.create_request()
                    request.release_pokemon(pokemon_id=pokemon.unique_id)
                    response_dict = request.call()
                else:
                    response_dict = {"responses": {"RELEASE_POKEMON": {"candy_awarded": 0}}}

                if not response_dict:
                    return False

                candy_awarded = response_dict.get("responses", {}).get("RELEASE_POKEMON", {}).get("candy_awarded", 0)
                candy = inventory.candies().get(pokemon.pokemon_id)

                if self.config_transfer and (not self.bot.config.test):
                    candy.add(candy_awarded)

                self.emit_event("pokemon_release",
                                formatted="Exchanged {pokemon} [IV {iv}] [CP {cp}] [{candy} candies]",
                                data={"pokemon": pokemon.name,
                                      "iv": pokemon.iv,
                                      "cp": pokemon.cp,
                                      "candy": candy.quantity})

                if self.config_transfer and (not self.bot.config.test):
                    inventory.pokemons().remove(pokemon.unique_id)

                    with self.bot.database as db:
                        cursor = db.cursor()
                        cursor.execute("SELECT COUNT(name) FROM sqlite_master WHERE type='table' AND name='transfer_log'")

                        db_result = cursor.fetchone()

                        if db_result[0] == 1:
                            db.execute("INSERT INTO transfer_log (pokemon, iv, cp) VALUES (?, ?, ?)", (pokemon.name, pokemon.iv, pokemon.cp))
                    if not skip_delay:
                        action_delay(self.config_action_wait_min, self.config_action_wait_max)

        return True


