import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
data=[("vid_001 Skincare",88,"SCALE"),("vid_003 Arepa recipe",81,"SCALE"),("vid_004 Headphones, 9h old",62,"DEFER TO HUMAN"),
("vid_006 Missing metrics",52,"DEFER TO HUMAN"),("vid_005 Tiny reach (640)",32,"DEFER TO HUMAN"),("vid_002 Leg workout",28,"DO NOT SCALE"),
("vid_008 Audience mismatch",28,"DO NOT SCALE"),("vid_007 Bought engagement",4,"FLAG AUTHENTICITY")]
col={"SCALE":"#1DB97A","DEFER TO HUMAN":"#F5A623","DO NOT SCALE":"#9AA0A6","FLAG AUTHENTICITY":"#E5484D"}
plt.rcParams["font.family"]="DejaVu Sans"
fig,ax=plt.subplots(figsize=(9,4.6),dpi=200)
labels=[d[0] for d in data][::-1]; vals=[d[1] for d in data][::-1]; cs=[col[d[2]] for d in data][::-1]
b=ax.barh(labels,vals,color=cs,height=0.62)
for r,v in zip(b,vals): ax.text(v+1.2,r.get_y()+r.get_height()/2,str(v),va="center",fontsize=10,color="#1A1A2E",fontweight="bold")
ax.set_xlim(0,100); ax.set_xlabel("P5 self-reported scale-worthiness score (0–100)",color="#4A4A6A")
for s in ["top","right"]: ax.spines[s].set_visible(False)
ax.spines["left"].set_color("#E0E0E0"); ax.spines["bottom"].set_color("#E0E0E0")
ax.tick_params(colors="#4A4A6A"); ax.xaxis.grid(True,color="#EEEEEE"); ax.set_axisbelow(True)
from matplotlib.patches import Patch
ax.legend(handles=[Patch(color=c,label=k) for k,c in col.items()],loc="lower right",frameon=False,fontsize=8.5)
ax.set_title("Reference chart (code-rendered from outputs/generations.jsonl)",loc="left",fontsize=11,color="#1A1A2E")
fig.tight_layout(); fig.savefig("REF_scorecard.png")
