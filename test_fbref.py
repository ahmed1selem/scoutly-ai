import soccerdata as sd
import json

def test():
    try:
        fbref = sd.FBref(leagues="Big 5 European Leagues Combined", seasons="2425")
        stats = fbref.read_player_season_stats(stat_type="standard")
        print("Columns:", stats.columns.tolist())
        
        # Reset index to make 'player', 'team', 'league' available as columns
        df = stats.reset_index()
        print("All Columns:", df.columns.tolist())
        
        # Example: print first 2 rows
        print("Sample data:", df.head(2).to_dict(orient="records"))
        
    except Exception as e:
        print("Exception:", e)

if __name__ == "__main__":
    test()
